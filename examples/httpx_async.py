"""Fetch many URLs concurrently, rotating proxies, with a health check first.

PROXY_FILE=proxies.txt python examples/httpx_async.py
"""

from __future__ import annotations

import asyncio
import os

from proxy_pool import ProxyPool, load_file
from proxy_pool.health import check_pool
from proxy_pool.httpx_adapter import AsyncPooledClient

URLS = ["https://api.quanticdata.io/v1/public/ip-echo"] * 10


async def main() -> None:
    pool = ProxyPool(load_file(os.environ.get("PROXY_FILE", "proxies.txt")), strategy="least_used")
    results = await check_pool(pool)
    print(f"{sum(r.ok for r in results)}/{len(results)} proxies answered")
    async with AsyncPooledClient(pool, timeout=30) as client:
        responses = await asyncio.gather(*(client.get(url) for url in URLS), return_exceptions=True)
    for item in responses:
        print(item if isinstance(item, BaseException) else item.json().get("payload", {}).get("ip"))


if __name__ == "__main__":
    asyncio.run(main())
