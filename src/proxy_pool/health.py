"""Concurrent health checks that feed results back into the pool.

Needs ``pip install "proxy-pool[httpx]"``. For a full audit of a proxy list
(latency percentiles, geolocation, anonymity, CSV reports) use the standalone
https://github.com/quanticdata-io/proxy-checker instead; this module only answers
"which proxies in my pool work right now" before or during a run.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from dataclasses import dataclass

import httpx

from .pool import Outcome, ProxyPool
from .proxy import Proxy

DEFAULT_CHECK_URL = "https://api.quanticdata.io/v1/public/ip-echo"
"""Free IP echo that needs no API key (https://quanticdata.io/docs/). Any URL
that answers 2xx works, for example your own target's robots.txt."""


@dataclass(frozen=True)
class HealthResult:
    proxy: Proxy
    ok: bool
    status: int | None
    elapsed: float
    error: str | None = None


async def check_proxy(proxy: Proxy, url: str = DEFAULT_CHECK_URL, timeout: float = 15.0) -> HealthResult:
    """One GET through ``proxy``; ok when the answer is 2xx or 3xx."""
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(proxy=proxy.url, timeout=timeout) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        return HealthResult(proxy, False, None, time.perf_counter() - started, f"{type(exc).__name__}: {exc}")
    ok = 200 <= response.status_code < 400
    return HealthResult(proxy, ok, response.status_code, time.perf_counter() - started)


async def check_all(
    proxies: Iterable[Proxy], url: str = DEFAULT_CHECK_URL, timeout: float = 15.0, concurrency: int = 20
) -> list[HealthResult]:
    """Check proxies concurrently, results in input order."""
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    gate = asyncio.Semaphore(concurrency)

    async def one(proxy: Proxy) -> HealthResult:
        async with gate:
            return await check_proxy(proxy, url, timeout)

    return list(await asyncio.gather(*(one(p) for p in proxies)))


async def check_pool(
    pool: ProxyPool, url: str = DEFAULT_CHECK_URL, timeout: float = 15.0, concurrency: int = 20
) -> list[HealthResult]:
    """Check every proxy in ``pool`` and report each result to it.

    Failing proxies start their cooldown (and are marked dead after
    ``max_failures`` checks in a row); working ones have their streak reset.
    """
    results = await check_all(pool.proxies, url, timeout, concurrency)
    for result in results:
        pool.report(result.proxy, Outcome.SUCCESS if result.ok else Outcome.ERROR)
    return results


def check_pool_sync(
    pool: ProxyPool, url: str = DEFAULT_CHECK_URL, timeout: float = 15.0, concurrency: int = 20
) -> list[HealthResult]:
    """:func:`check_pool` for code that is not async."""
    return asyncio.run(check_pool(pool, url, timeout, concurrency))
