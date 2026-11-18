"""Scrapy middleware: unit tests on Request/Response objects, plus one real crawl
in a subprocess against in-process fake proxies."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

pytest.importorskip("scrapy")

from scrapy import Request
from scrapy.exceptions import IgnoreRequest, NotConfigured
from scrapy.http import HtmlResponse
from scrapy.utils.test import get_crawler
from twisted.internet.error import ConnectionRefusedError

from conftest import FakeClock, FakeProxy
from proxy_pool import BanDetector, ProxyPool
from proxy_pool.scrapy_middleware import META_PROXY, META_RETRIES, ProxyPoolMiddleware

ROOT = Path(__file__).resolve().parents[1]


def run(coro: object) -> None:
    asyncio.run(coro)  # type: ignore[arg-type]


def middleware(pool: ProxyPool, **kwargs: object) -> ProxyPoolMiddleware:
    return ProxyPoolMiddleware(pool, **kwargs)  # type: ignore[arg-type]


def response(request: Request, status: int, body: bytes = b"ok") -> HtmlResponse:
    return HtmlResponse(request.url, status=status, body=body, request=request)


def test_sets_proxy_meta() -> None:
    mw = middleware(ProxyPool(["u:p@h1:1", "h2:2"]))
    request = Request("http://target.test/")
    run(mw.process_request(request))
    assert request.meta["proxy"] == "http://u:p@h1:1"
    assert request.meta[META_PROXY].host == "h1"
    assert "download_slot" not in request.meta


def test_slot_per_proxy_is_opt_in() -> None:
    mw = middleware(ProxyPool(["h1:1"]), slot_per_proxy=True)
    request = Request("http://target.test/")
    run(mw.process_request(request))
    assert request.meta["download_slot"] == "proxy_pool:http://h1:1"


def test_respects_a_proxy_chosen_by_the_spider() -> None:
    mw = middleware(ProxyPool(["h1:1"]))
    request = Request("http://target.test/", meta={"proxy": "http://mine:8"})
    run(mw.process_request(request))
    assert request.meta["proxy"] == "http://mine:8"
    skipped = Request("http://target.test/", meta={"proxy_pool_skip": True})
    run(mw.process_request(skipped))
    assert "proxy" not in skipped.meta


def test_ban_is_retried_with_clean_proxy_state() -> None:
    pool = ProxyPool(["h1:1", "h2:2"])
    mw = middleware(pool)
    request = Request("http://target.test/", headers={"Proxy-Authorization": "Basic x"})
    run(mw.process_request(request))
    retry = mw.process_response(request, response(request, 403))
    assert isinstance(retry, Request)
    assert retry.dont_filter
    assert retry.meta[META_RETRIES] == 1
    assert "proxy" not in retry.meta
    assert b"Proxy-Authorization" not in retry.headers
    run(mw.process_request(retry))
    assert retry.meta["proxy"] == "http://h2:2"
    assert pool.stats()[0].bans == 1


def test_success_passes_through() -> None:
    pool = ProxyPool(["h1:1"])
    mw = middleware(pool)
    request = Request("http://target.test/")
    run(mw.process_request(request))
    ok = response(request, 200)
    assert mw.process_response(request, ok) is ok
    assert pool.stats()[0].successes == 1


def test_body_marker_ban() -> None:
    mw = middleware(ProxyPool(["h1:1", "h2:2"]), detector=BanDetector.build(markers=["captcha"]))
    request = Request("http://target.test/")
    run(mw.process_request(request))
    assert isinstance(
        mw.process_response(request, response(request, 200, b"<title>CAPTCHA</title>")), Request
    )


def test_connection_error_retried_and_others_ignored() -> None:
    pool = ProxyPool(["h1:1", "h2:2"])
    mw = middleware(pool)
    request = Request("http://target.test/")
    run(mw.process_request(request))
    refused = ConnectionRefusedError()  # type: ignore[no-untyped-call]
    assert isinstance(mw.process_exception(request, refused), Request)
    assert mw.process_exception(request, ValueError("parser bug")) is None
    assert pool.stats()[0].errors == 1


def test_gives_up_after_max_retries() -> None:
    mw = middleware(ProxyPool(["h1:1"], cooldown=0, max_failures=None), max_retries=1)
    request = Request("http://target.test/", meta={META_RETRIES: 1})
    run(mw.process_request(request))
    with pytest.raises(IgnoreRequest, match="gave up"):
        mw.process_response(request, response(request, 429))


def test_sticky_domain_keeps_one_proxy_per_host() -> None:
    mw = middleware(ProxyPool(["h1:1", "h2:2", "h3:3"]), sticky="domain")
    proxies = set()
    for path in range(4):
        request = Request(f"http://shop.test/{path}")
        run(mw.process_request(request))
        proxies.add(request.meta["proxy"])
    other = Request("http://news.test/")
    run(mw.process_request(other))
    assert len(proxies) == 1
    assert other.meta["proxy"] not in proxies


def test_meta_session_key() -> None:
    mw = middleware(ProxyPool(["h1:1", "h2:2"]))
    seen = set()
    for _ in range(3):
        request = Request("http://target.test/", meta={"proxy_session_key": "account-7"})
        run(mw.process_request(request))
        seen.add(request.meta["proxy"])
    assert len(seen) == 1


def test_waits_for_cooldown_then_gives_up(clock: FakeClock) -> None:
    pool = ProxyPool(["h1:1"], clock=clock, cooldown=5, jitter=0, max_failures=None)
    pool.failure(pool.proxies[0])
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.advance(seconds)

    mw = middleware(pool, sleep=fake_sleep, max_wait=10)
    request = Request("http://target.test/")
    run(mw.process_request(request))
    assert slept == [5]
    assert request.meta["proxy"] == "http://h1:1"

    pool.failure(pool.proxies[0])
    pool.failure(pool.proxies[0])  # 10 s cooldown now, more than what is left of max_wait
    mw = middleware(pool, sleep=fake_sleep, max_wait=4)
    with pytest.raises(IgnoreRequest, match="PROXY_POOL_MAX_WAIT"):
        run(mw.process_request(Request("http://target.test/")))


def test_from_crawler_settings(tmp_path: Path) -> None:
    listing = tmp_path / "proxies.txt"
    listing.write_text("h2:2\n", encoding="utf-8")
    crawler = get_crawler(
        settings_dict={
            "PROXY_POOL_LIST": ["h1:1"],
            "PROXY_POOL_FILE": str(listing),
            "PROXY_POOL_STICKY": "domain",
            "PROXY_POOL_BAN_STATUSES": [403, 503],
            "PROXY_POOL_MAX_FAILURES": 0,
            "PROXY_POOL_STICKY_TTL": 0,
        }
    )
    mw = ProxyPoolMiddleware.from_crawler(crawler)
    assert [p.host for p in mw.pool.proxies] == ["h1", "h2"]
    assert mw.sticky == "domain"
    assert mw.detector.statuses == {403, 503}
    assert mw.pool.max_failures is None
    assert mw.pool.sticky_ttl is None


def test_not_configured_without_proxies() -> None:
    with pytest.raises(NotConfigured):
        ProxyPoolMiddleware.from_crawler(get_crawler())
    with pytest.raises(NotConfigured):
        ProxyPoolMiddleware.from_crawler(get_crawler(settings_dict={"PROXY_POOL_ENABLED": False}))


SPIDER = """
import scrapy

class Spider(scrapy.Spider):
    name = "t"
    custom_settings = {{
        "DOWNLOADER_MIDDLEWARES": {{"proxy_pool.scrapy_middleware.ProxyPoolMiddleware": 610}},
        "PROXY_POOL_LIST": {proxies!r},
        "PROXY_POOL_COOLDOWN": 60,
        "RETRY_ENABLED": False,
        "LOG_LEVEL": "WARNING",
        "TELNETCONSOLE_ENABLED": False,
    }}

    async def start(self):
        for i in range(6):
            yield scrapy.Request(f"http://target.test/item/{{i}}")

    def parse(self, response):
        yield response.json()
"""


def test_real_crawl_rotates_and_recovers(
    tmp_path: Path, make_proxy: Callable[..., FakeProxy], dead_proxy_url: str
) -> None:
    banned = make_proxy("banned", "ban")
    good = make_proxy("good", credentials=("user-country-us", "s3cret"))
    spider = tmp_path / "spider.py"
    spider.write_text(textwrap.dedent(SPIDER.format(proxies=[banned.url, dead_proxy_url, good.url])), "utf-8")
    out = tmp_path / "items.jsonl"
    result = subprocess.run(
        [sys.executable, "-m", "scrapy", "runspider", str(spider), "-o", str(out)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    items = [json.loads(line) for line in out.read_text("utf-8").splitlines()]
    assert len(items) == 6, result.stderr
    assert {item["via"] for item in items} == {"good"}
    assert 1 <= len(banned.hits) <= 2  # the first wave goes out before any ban is seen, then it cools down
