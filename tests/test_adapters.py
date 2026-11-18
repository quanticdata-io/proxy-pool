"""requests and httpx adapters against in-process fake proxies."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx
import pytest
import requests

from conftest import FakeProxy
from proxy_pool import BanDetector, NoProxyAvailable, ProxyPool
from proxy_pool.httpx_adapter import AsyncPooledClient, PooledClient
from proxy_pool.requests_adapter import PooledSession

TARGET = "http://target.test/page"

MakeProxy = Callable[..., FakeProxy]


def test_requests_retries_ban_through_another_proxy(make_proxy: MakeProxy) -> None:
    banned, good = make_proxy("banned", "ban"), make_proxy("good")
    pool = ProxyPool([banned.url, good.url])
    with PooledSession(pool) as session:
        response = session.get(TARGET, timeout=5)
    assert response.status_code == 200
    assert response.json() == {"via": "good", "url": TARGET}
    assert banned.hits == [TARGET]
    stats = {s.proxy.port: s for s in pool.stats()}
    assert stats[banned.port].bans == 1
    assert stats[good.port].successes == 1


def test_requests_retries_connection_errors(make_proxy: MakeProxy, dead_proxy_url: str) -> None:
    good = make_proxy("good")
    pool = ProxyPool([dead_proxy_url, good.url])
    with PooledSession(pool) as session:
        assert session.get(TARGET, timeout=5).json()["via"] == "good"
    assert pool.stats()[0].errors == 1


def test_requests_sends_proxy_credentials(make_proxy: MakeProxy) -> None:
    secured = make_proxy("secured", credentials=("user-country-us", "p@ss:word"))
    pool = ProxyPool([secured.url])
    with PooledSession(pool) as session:
        assert session.get(TARGET, timeout=5).status_code == 200


def test_requests_407_is_a_proxy_error_not_a_ban(make_proxy: MakeProxy) -> None:
    secured = make_proxy("secured", credentials=("u", "right"))
    wrong = secured.url.replace("right", "wrong")
    pool = ProxyPool([wrong])
    with PooledSession(pool, max_attempts=1) as session:
        assert session.get(TARGET, timeout=5).status_code == 407
    assert pool.stats()[0].errors == 1
    assert pool.stats()[0].bans == 0


def test_requests_returns_last_ban_when_attempts_run_out(make_proxy: MakeProxy) -> None:
    pool = ProxyPool([make_proxy("a", "ban").url, make_proxy("b", "ban").url], cooldown=0, max_failures=None)
    with PooledSession(pool, max_attempts=2) as session:
        assert session.get(TARGET, timeout=5).status_code == 403


def test_requests_does_not_retry_post(make_proxy: MakeProxy) -> None:
    banned, good = make_proxy("banned", "ban"), make_proxy("good")
    pool = ProxyPool([banned.url, good.url])
    with PooledSession(pool) as session:
        assert session.post(TARGET, data=b"x", timeout=5).status_code == 403
    assert good.hits == []


def test_requests_body_markers(make_proxy: MakeProxy) -> None:
    captcha, good = make_proxy("captcha", "captcha"), make_proxy("good")
    pool = ProxyPool([captcha.url, good.url])
    detector = BanDetector.build(markers=["captcha"])
    with PooledSession(pool, detector=detector) as session:
        assert session.get(TARGET, timeout=5).json()["via"] == "good"


def test_requests_sticky_session_key(make_proxy: MakeProxy) -> None:
    proxies = [make_proxy(f"p{i}") for i in range(3)]
    pool = ProxyPool([p.url for p in proxies])
    with PooledSession(pool) as session:
        seen = {session.get(TARGET, session_key="acct", timeout=5).json()["via"] for _ in range(5)}
    assert len(seen) == 1


def test_requests_refuses_explicit_proxies(make_proxy: MakeProxy) -> None:
    pool = ProxyPool([make_proxy("a").url])
    with PooledSession(pool) as session, pytest.raises(ValueError, match="proxies"):
        session.get(TARGET, proxies={"http": "http://x:1"})


def test_requests_raises_when_every_proxy_is_dead(dead_proxy_url: str) -> None:
    pool = ProxyPool([dead_proxy_url], max_failures=1)
    with PooledSession(pool, max_attempts=3) as session, pytest.raises(NoProxyAvailable):
        session.get(TARGET, timeout=5)


def test_httpx_sync_rotation_and_retry(make_proxy: MakeProxy, dead_proxy_url: str) -> None:
    banned, good = make_proxy("banned", "ban"), make_proxy("good")
    pool = ProxyPool([dead_proxy_url, banned.url, good.url])
    with PooledClient(pool, timeout=5) as client:
        response = client.get(TARGET)
    assert response.json()["via"] == "good"
    assert [s.errors + s.bans for s in pool.stats()] == [1, 1, 0]


def test_httpx_sync_raises_last_transport_error(dead_proxy_url: str) -> None:
    pool = ProxyPool([dead_proxy_url], cooldown=0, max_failures=None)
    with PooledClient(pool, timeout=5, max_attempts=2) as client, pytest.raises(httpx.TransportError):
        client.get(TARGET)


def test_httpx_rejects_proxy_argument() -> None:
    with pytest.raises(ValueError, match="proxy"):
        PooledClient(ProxyPool(["h:1"]), proxy="http://x:1")


def test_httpx_async_concurrent(make_proxy: MakeProxy) -> None:
    proxies = [make_proxy(f"p{i}") for i in range(3)]
    pool = ProxyPool([p.url for p in proxies])

    async def run() -> list[str]:
        async with AsyncPooledClient(pool, timeout=5) as client:
            responses = await asyncio.gather(*(client.get(TARGET) for _ in range(9)))
            ban_free = await client.post(TARGET, content=b"x")
            assert ban_free.status_code == 200
        return [r.json()["via"] for r in responses]

    vias = asyncio.run(run())
    assert sorted(set(vias)) == ["p0", "p1", "p2"]


def test_httpx_async_retries_ban(make_proxy: MakeProxy) -> None:
    pool = ProxyPool([make_proxy("banned", "ban").url, make_proxy("good").url])

    async def run() -> str:
        async with AsyncPooledClient(pool, timeout=5) as client:
            return str((await client.get(TARGET)).json()["via"])

    assert asyncio.run(run()) == "good"


def test_requests_session_is_a_requests_session(make_proxy: MakeProxy) -> None:
    session = PooledSession(ProxyPool([make_proxy("a").url]))
    assert isinstance(session, requests.Session)
    with pytest.raises(ValueError, match="max_attempts"):
        PooledSession(ProxyPool(["h:1"]), max_attempts=0)
    session.close()
