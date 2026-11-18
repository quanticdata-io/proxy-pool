"""Scrapy downloader middleware: rotate proxies, keep sticky sessions, retry blocks.

Enable it in ``settings.py``::

    DOWNLOADER_MIDDLEWARES = {
        "proxy_pool.scrapy_middleware.ProxyPoolMiddleware": 610,
    }
    PROXY_POOL_FILE = "proxies.txt"

Priority 610 runs it before Scrapy's ``HttpProxyMiddleware`` (750), which turns
the credentials in the proxy URL into a ``Proxy-Authorization`` header. Every
setting is listed in the README.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import scrapy.exceptions as _scrapy_exceptions
from scrapy import Request, Spider
from scrapy.crawler import Crawler
from scrapy.exceptions import IgnoreRequest, NotConfigured
from scrapy.http import Response
from scrapy.settings import BaseSettings
from twisted.internet.error import (
    ConnectError,
    ConnectionLost,
    ConnectionRefusedError,
    DNSLookupError,
    TCPTimedOutError,
    TimeoutError,
)
from twisted.web.client import ResponseFailed

from .bans import BanDetector
from .pool import NoProxyAvailable, Outcome, ProxyPool
from .proxy import Proxy, load_file, parse_proxy

logger = logging.getLogger(__name__)

META_PROXY = "_proxy_pool_proxy"
META_RETRIES = "proxy_pool_retries"
META_KEY = "proxy_session_key"
META_SKIP = "proxy_pool_skip"

# Scrapy 2.13+ wraps Twisted errors in its own exceptions; older versions raise
# the Twisted ones directly. Both are covered.
CONNECTION_ERRORS: tuple[type[BaseException], ...] = (
    *(
        getattr(_scrapy_exceptions, name)
        for name in (
            "DownloadConnectionRefusedError",
            "CannotResolveHostError",
            "DownloadFailedError",
            "DownloadTimeoutError",
        )
        if hasattr(_scrapy_exceptions, name)
    ),
    ConnectError,
    ConnectionLost,
    ConnectionRefusedError,
    DNSLookupError,
    TCPTimedOutError,
    TimeoutError,
    ResponseFailed,
    OSError,
)


def _is_connection_error(exc: BaseException) -> bool:
    seen = 0
    current: BaseException | None = exc
    while current is not None and seen < 5:
        if isinstance(current, CONNECTION_ERRORS):
            return True
        current = current.__cause__
        seen += 1
    return False


def _default_sleep(seconds: float) -> Awaitable[None]:
    from scrapy.utils.defer import maybe_deferred_to_future
    from twisted.internet import reactor
    from twisted.internet.task import deferLater

    return maybe_deferred_to_future(deferLater(reactor, seconds, lambda: None))


class ProxyPoolMiddleware:
    """See the module docstring and the README for settings."""

    def __init__(
        self,
        pool: ProxyPool,
        *,
        detector: BanDetector | None = None,
        sticky: str = "none",
        max_retries: int = 5,
        max_wait: float = 60.0,
        slot_per_proxy: bool = False,
        stats: Any = None,
        sleep: Callable[[float], Awaitable[None]] = _default_sleep,
    ) -> None:
        if sticky not in ("none", "domain"):
            raise ValueError("PROXY_POOL_STICKY must be 'none' or 'domain'")
        self.pool = pool
        self.detector = detector or BanDetector()
        self.sticky = sticky
        self.max_retries = max_retries
        self.max_wait = max_wait
        self.slot_per_proxy = slot_per_proxy
        self.stats = stats
        self._sleep = sleep

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> ProxyPoolMiddleware:
        settings = crawler.settings
        if not settings.getbool("PROXY_POOL_ENABLED", True):
            raise NotConfigured("PROXY_POOL_ENABLED is False")
        proxies = cls._load_proxies(settings)
        if not proxies:
            raise NotConfigured("no proxies: set PROXY_POOL_LIST, PROXY_POOL_FILE or PROXY_POOL_QUANTICDATA")
        max_failures = settings.getint("PROXY_POOL_MAX_FAILURES", 5)
        ttl = settings.getfloat("PROXY_POOL_STICKY_TTL", 600.0)
        pool = ProxyPool(
            proxies,
            strategy=settings.get("PROXY_POOL_STRATEGY", "round_robin"),
            sticky_ttl=ttl if ttl > 0 else None,
            cooldown=settings.getfloat("PROXY_POOL_COOLDOWN", 30.0),
            max_cooldown=settings.getfloat("PROXY_POOL_MAX_COOLDOWN", 600.0),
            max_failures=max_failures if max_failures > 0 else None,
        )
        statuses = settings.getlist("PROXY_POOL_BAN_STATUSES") or None
        detector = BanDetector.build(
            [int(s) for s in statuses] if statuses else None, settings.getlist("PROXY_POOL_BAN_MARKERS")
        )
        logger.info("proxy pool: %d proxies loaded", len(pool))
        return cls(
            pool,
            detector=detector,
            sticky=settings.get("PROXY_POOL_STICKY", "none"),
            max_retries=settings.getint("PROXY_POOL_MAX_RETRIES", 5),
            max_wait=settings.getfloat("PROXY_POOL_MAX_WAIT", 60.0),
            slot_per_proxy=settings.getbool("PROXY_POOL_SLOT_PER_PROXY", False),
            stats=crawler.stats,
        )

    @staticmethod
    def _load_proxies(settings: BaseSettings) -> list[Proxy]:
        scheme = settings.get("PROXY_POOL_DEFAULT_SCHEME", "http")
        proxies = [parse_proxy(p, scheme) for p in settings.getlist("PROXY_POOL_LIST")]
        path = settings.get("PROXY_POOL_FILE")
        if path:
            proxies.extend(load_file(path, scheme))
        qd = settings.getdict("PROXY_POOL_QUANTICDATA")
        if qd:
            from .quanticdata import from_quanticdata

            options = dict(qd)
            order_id = str(options.pop("order_id"))
            proxies.extend(from_quanticdata(order_id, **options))
        return proxies

    # -- helpers ----------------------------------------------------------

    def _inc(self, name: str) -> None:
        if self.stats is not None:
            self.stats.inc_value(f"proxy_pool/{name}")

    def _key(self, request: Request) -> str | None:
        key = request.meta.get(META_KEY)
        if key is not None:
            return str(key)
        if self.sticky == "domain":
            return urlsplit(request.url).hostname
        return None

    def _proxy_of(self, request: Request) -> Proxy | None:
        value = request.meta.get(META_PROXY)
        return value if isinstance(value, Proxy) else None

    def _retry(self, request: Request, reason: str) -> Request:
        retries = int(request.meta.get(META_RETRIES, 0)) + 1
        if retries > self.max_retries:
            self._inc("gave_up")
            raise IgnoreRequest(
                f"proxy pool: gave up on {request.url} after {self.max_retries} retries ({reason})"
            )
        new: Request = request.replace(dont_filter=True)
        new.meta[META_RETRIES] = retries
        for meta_key in (META_PROXY, "proxy", "_auth_proxy", "download_slot"):
            new.meta.pop(meta_key, None)
        if b"Proxy-Authorization" in new.headers:
            del new.headers[b"Proxy-Authorization"]
        new.priority = request.priority + 1
        self._inc(f"retry/{reason}")
        logger.debug("proxy pool: retrying %s (%s), attempt %d", request.url, reason, retries + 1)
        return new

    # -- Scrapy hooks -----------------------------------------------------

    async def process_request(self, request: Request, spider: Spider | None = None) -> None:
        if request.meta.get(META_SKIP) or (request.meta.get("proxy") and META_PROXY not in request.meta):
            return  # the spider chose its own proxy (or none) for this request
        key = self._key(request)
        waited = 0.0
        while True:
            try:
                proxy = self.pool.acquire(key)
                break
            except NoProxyAvailable as exc:
                if exc.retry_after is None:
                    self._inc("exhausted")
                    raise IgnoreRequest("proxy pool: every proxy is dead") from exc
                if waited + exc.retry_after > self.max_wait:
                    self._inc("exhausted")
                    raise IgnoreRequest(
                        f"proxy pool: no proxy free within PROXY_POOL_MAX_WAIT ({self.max_wait}s)"
                    ) from exc
                waited += exc.retry_after
                await self._sleep(exc.retry_after)
        request.meta[META_PROXY] = proxy
        request.meta["proxy"] = proxy.url
        if self.slot_per_proxy:
            # One download slot per proxy: Scrapy's per-slot delay and
            # concurrency then apply to each proxy instead of to each domain.
            request.meta["download_slot"] = f"proxy_pool:{proxy.server}"
        self._inc("requests")
        return

    def process_response(
        self, request: Request, response: Response, spider: Spider | None = None
    ) -> Request | Response:
        proxy = self._proxy_of(request)
        if proxy is None:
            return response
        if BanDetector.is_proxy_error(response.status):
            self.pool.report(proxy, Outcome.ERROR)
            return self._retry(request, "proxy_auth")
        body = response.body if self.detector.markers else None
        if self.detector.is_ban(response.status, body):
            self.pool.report(proxy, Outcome.BAN)
            self._inc("bans")
            return self._retry(request, f"ban_{response.status}")
        self.pool.report(proxy, Outcome.SUCCESS)
        return response

    def process_exception(
        self, request: Request, exception: BaseException, spider: Spider | None = None
    ) -> Request | None:
        proxy = self._proxy_of(request)
        if proxy is None or not _is_connection_error(exception):
            return None
        self.pool.report(proxy, Outcome.ERROR)
        self._inc("errors")
        return self._retry(request, "error")
