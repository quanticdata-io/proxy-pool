from __future__ import annotations

import random
import threading
from collections import Counter

import pytest

from conftest import FakeClock
from proxy_pool import NoProxyAvailable, Outcome, ProxyPool, Strategy, parse_proxy

LIST = ["h1:1", "h2:2", "h3:3"]


def make(clock: FakeClock, **kwargs: object) -> ProxyPool:
    kwargs.setdefault("jitter", 0.0)
    return ProxyPool(LIST, clock=clock, rng=random.Random(7), **kwargs)  # type: ignore[arg-type]


def test_round_robin_cycles_in_order(clock: FakeClock) -> None:
    pool = make(clock)
    assert [pool.acquire().host for _ in range(6)] == ["h1", "h2", "h3", "h1", "h2", "h3"]


def test_random_uses_every_proxy(clock: FakeClock) -> None:
    pool = make(clock, strategy="random")
    assert pool.strategy is Strategy.RANDOM
    assert {pool.acquire().host for _ in range(60)} == {"h1", "h2", "h3"}


def test_least_used_balances(clock: FakeClock) -> None:
    pool = make(clock, strategy=Strategy.LEAST_USED)
    pool.acquire(key="pinned")  # h1 used once
    counts = Counter(pool.acquire().host for _ in range(5))
    assert counts == {"h2": 2, "h3": 2, "h1": 1}


def test_sticky_key_keeps_its_proxy_until_ttl(clock: FakeClock) -> None:
    pool = make(clock, sticky_ttl=60)
    first = pool.acquire("account-1")
    other = pool.acquire("account-2")
    assert other != first
    assert all(pool.acquire("account-1") == first for _ in range(5))
    assert pool.bound("account-1") == first
    clock.advance(61)
    assert pool.bound("account-1") is None
    assert pool.acquire("account-1") != first or len(pool) == 1


def test_sticky_without_ttl_lasts_until_release(clock: FakeClock) -> None:
    pool = make(clock, sticky_ttl=None)
    first = pool.acquire("k")
    clock.advance(10**6)
    assert pool.acquire("k") == first
    pool.release("k")
    assert pool.bound("k") is None


def test_failure_moves_sticky_key_and_cools_down(clock: FakeClock) -> None:
    pool = make(clock, cooldown=10, max_cooldown=100)
    first = pool.acquire("k")
    pool.report(first, Outcome.BAN)
    second = pool.acquire("k")
    assert second != first
    assert first not in [pool.acquire() for _ in range(4)]
    clock.advance(10)
    assert first in [pool.acquire() for _ in range(3)]


def test_backoff_doubles_and_is_capped(clock: FakeClock) -> None:
    pool = ProxyPool(["h1:1"], clock=clock, cooldown=10, max_cooldown=35, max_failures=None, jitter=0)
    proxy = pool.proxies[0]
    waits = []
    for _ in range(4):
        pool.failure(proxy)
        waits.append(pool.wait_time())
        clock.advance(1000)
    assert waits == [10, 20, 35, 35]


def test_success_resets_the_streak(clock: FakeClock) -> None:
    pool = ProxyPool(["h1:1"], clock=clock, cooldown=10, jitter=0, max_failures=3)
    proxy = pool.proxies[0]
    pool.failure(proxy)
    pool.failure(proxy)
    pool.success(proxy)
    assert pool.wait_time() == 0
    pool.failure(proxy)
    assert pool.wait_time() == 10
    assert pool.stats()[0].consecutive_failures == 1


def test_dead_after_max_failures_and_revive(clock: FakeClock) -> None:
    pool = ProxyPool(["h1:1"], clock=clock, max_failures=2)
    proxy = pool.proxies[0]
    pool.failure(proxy)
    pool.failure(proxy)
    with pytest.raises(NoProxyAvailable) as info:
        pool.acquire()
    assert info.value.retry_after is None
    assert pool.summary()["dead"] == 1
    pool.revive()
    assert pool.acquire() == proxy


def test_no_proxy_available_reports_retry_after(clock: FakeClock) -> None:
    pool = ProxyPool(["h1:1", "h2:2"], clock=clock, cooldown=5, jitter=0)
    for proxy in pool.proxies:
        pool.failure(proxy)
    clock.advance(2)
    with pytest.raises(NoProxyAvailable) as info:
        pool.acquire()
    assert info.value.retry_after == pytest.approx(3)


def test_jitter_spreads_cooldowns(clock: FakeClock) -> None:
    pool = ProxyPool([f"h{i}:1" for i in range(20)], clock=clock, cooldown=100, jitter=0.5)
    for proxy in pool.proxies:
        pool.failure(proxy)
    ends = {s.cooldown_until for s in pool.stats()}
    assert len(ends) > 1
    assert all(clock.now + 100 <= e <= clock.now + 150 for e in ends)


def test_add_remove_and_summary(clock: FakeClock) -> None:
    pool = make(clock)
    assert pool.add(["h1:1", "h4:4"]) == 1
    assert len(pool) == 4
    h2 = parse_proxy("h2:2")
    pool.acquire("k")
    pool.remove(pool.bound("k") or h2)
    assert pool.bound("k") is None
    assert len(pool) == 3
    assert pool.summary() == {"total": 3, "available": 3, "cooling": 0, "dead": 0, "sticky_keys": 0}


def test_report_unknown_proxy_is_ignored(clock: FakeClock) -> None:
    pool = make(clock)
    pool.report(parse_proxy("other:9"), "error")
    assert pool.summary()["cooling"] == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cooldown": -1},
        {"cooldown": 10, "max_cooldown": 5},
        {"max_failures": 0},
        {"sticky_ttl": 0},
        {"jitter": 2},
    ],
)
def test_invalid_settings(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        ProxyPool(LIST, **kwargs)  # type: ignore[arg-type]


def test_thread_safety() -> None:
    pool = ProxyPool([f"h{i}:1" for i in range(10)])
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for i in range(500):
                proxy = pool.acquire(key=str(i % 7))
                pool.report(proxy, Outcome.SUCCESS if i % 5 else Outcome.ERROR)
                pool.revive()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert sum(s.uses for s in pool.stats()) == 8 * 500
