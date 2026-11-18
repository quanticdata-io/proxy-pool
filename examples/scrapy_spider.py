"""A Scrapy spider that rotates proxies and keeps one sticky proxy per domain.

PROXY_FILE=proxies.txt scrapy runspider examples/scrapy_spider.py -o out.jsonl
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import scrapy
from scrapy.http import Response


class EchoSpider(scrapy.Spider):
    name = "echo"
    custom_settings = {  # noqa: RUF012 - Scrapy reads it as a class attribute
        "DOWNLOADER_MIDDLEWARES": {"proxy_pool.scrapy_middleware.ProxyPoolMiddleware": 610},
        "PROXY_POOL_FILE": os.environ.get("PROXY_FILE", "proxies.txt"),
        "PROXY_POOL_STICKY": "domain",
        "PROXY_POOL_BAN_MARKERS": ["captcha"],
    }

    async def start(self) -> AsyncIterator[scrapy.Request]:
        for _ in range(5):
            yield scrapy.Request("https://api.quanticdata.io/v1/public/ip-echo", dont_filter=True)

    def parse(self, response: Response, **kwargs: Any) -> Iterator[dict[str, Any]]:
        exit_info = json.loads(response.body)["payload"]
        yield {"proxy": response.meta.get("proxy", "").split("@")[-1], "echo": exit_info}
