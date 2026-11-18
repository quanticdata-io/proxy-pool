"""A thread-safe proxy pool with rotation strategies, sticky keys and backoff."""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum

from .proxy import Proxy, parse_proxy


class Strategy(str, Enum):
    """How :meth:`ProxyPool.acquire` picks a proxy when no sticky key applies."""

    ROUND_ROBIN = "round_robin"
    RANDOM = "random"
    LEAST_USED = "least_used"


class Outcome(str, Enum):
    """What happened when a proxy was used."""

    SUCCESS = "success"
    ERROR = "error"  # connection refused, timeout, TLS or proxy protocol error
    BAN = "ban"  # the target answered, but with a block (see BanDetector)


class NoProxyAvailable(RuntimeError):
    """Every proxy is cooling down or dead.

    ``retry_after`` is the number of seconds until the first proxy leaves its
    cooldown, or ``None`` when every proxy is dead.
    """

    def __init__(self, message: str, retry_after: float | None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class ProxyStats:
    """Counters for one proxy. A snapshot: changing it does not change the pool."""

    proxy: Proxy
    uses: int = 0
    successes: int = 0
    errors: int = 0
    bans: int = 0
    consecutive_failures: int = 0
    cooldown_until: float = 0.0
    dead: bool = False
    last_used: float | None = None

    def copy(self) -> ProxyStats:
        return ProxyStats(**self.__dict__)


@dataclass(frozen=True)
class _Binding:
    proxy: Proxy
    expires_at: float | None


class ProxyPool:
    """Hand out proxies, track how they behave, rest the ones that fail.

    Parameters
    ----------
    proxies:
        Proxy objects or proxy strings (any format :func:`parse_proxy` reads).
    strategy:
        ``round_robin`` (default), ``random`` or ``least_used``.
    sticky_ttl:
        Seconds a sticky key keeps its proxy. ``None`` keeps it until the proxy
        fails or :meth:`release` is called.
    cooldown:
        Seconds a proxy rests after its first failure. Each further consecutive
        failure doubles it, up to ``max_cooldown``. A success resets it.
    max_cooldown:
        Upper bound of the cooldown, in seconds.
    max_failures:
        Consecutive failures after which a proxy is marked dead and never handed
        out again (until :meth:`revive`). ``None`` never marks proxies dead.
    jitter:
        Fraction of random spread added to each cooldown (0.1 = up to +10%),
        so a batch of proxies that failed together does not come back together.
    clock, rng:
        Injected for tests; default to ``time.monotonic`` and ``random.Random()``.
    """

    def __init__(
        self,
        proxies: Iterable[Proxy | str],
        *,
        strategy: Strategy | str = Strategy.ROUND_ROBIN,
        sticky_ttl: float | None = 600.0,
        cooldown: float = 30.0,
        max_cooldown: float = 600.0,
        max_failures: int | None = 5,
        jitter: float = 0.1,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        if cooldown < 0 or max_cooldown < cooldown:
            raise ValueError("need 0 <= cooldown <= max_cooldown")
        if max_failures is not None and max_failures < 1:
            raise ValueError("max_failures must be at least 1 or None")
        if sticky_ttl is not None and sticky_ttl <= 0:
            raise ValueError("sticky_ttl must be positive or None")
        if not 0 <= jitter <= 1:
            raise ValueError("jitter must be between 0 and 1")
        self.strategy = Strategy(strategy)
        self.sticky_ttl = sticky_ttl
        self.cooldown = cooldown
        self.max_cooldown = max_cooldown
        self.max_failures = max_failures
        self.jitter = jitter
        self._clock = clock
        self._rng = rng or random.Random()
        self._lock = threading.Lock()
        self._stats: dict[Proxy, ProxyStats] = {}
        self._order: list[Proxy] = []
        self._cursor = 0
        self._bindings: dict[str, _Binding] = {}
        self.add(proxies)

    # -- membership -------------------------------------------------------

    def add(self, proxies: Iterable[Proxy | str]) -> int:
        """Add proxies; returns how many were new."""
        added = 0
        with self._lock:
            for item in proxies:
                proxy = item if isinstance(item, Proxy) else parse_proxy(item)
                if proxy in self._stats:
                    continue
                self._stats[proxy] = ProxyStats(proxy)
                self._order.append(proxy)
                added += 1
        return added

    def remove(self, proxy: Proxy) -> None:
        """Remove a proxy and every sticky key bound to it."""
        with self._lock:
            if proxy not in self._stats:
                return
            del self._stats[proxy]
            index = self._order.index(proxy)
            self._order.pop(index)
            if index < self._cursor:
                self._cursor -= 1
            self._unbind(proxy)

    def revive(self, proxy: Proxy | None = None) -> None:
        """Clear dead flags and cooldowns (of one proxy, or of all)."""
        with self._lock:
            targets = [self._stats[proxy]] if proxy is not None else list(self._stats.values())
            for stat in targets:
                stat.dead = False
                stat.cooldown_until = 0.0
                stat.consecutive_failures = 0

    def __len__(self) -> int:
        return len(self._order)

    def __contains__(self, proxy: object) -> bool:
        return proxy in self._stats

    @property
    def proxies(self) -> list[Proxy]:
        return list(self._order)

    # -- handing out ------------------------------------------------------

    def acquire(self, key: str | None = None) -> Proxy:
        """Return a usable proxy.

        With ``key`` (a domain, an account, a browser profile...) the same proxy
        is returned for that key until it fails, its binding expires
        (``sticky_ttl``) or :meth:`release` is called. Without a key the
        strategy picks one.

        Raises :class:`NoProxyAvailable` when every proxy is resting or dead.
        """
        with self._lock:
            now = self._clock()
            if key is not None:
                binding = self._bindings.get(key)
                if binding is not None:
                    stat = self._stats.get(binding.proxy)
                    expired = binding.expires_at is not None and now >= binding.expires_at
                    if stat is not None and not expired and self._usable(stat, now):
                        return self._use(stat, now)
                    del self._bindings[key]
            stat = self._pick(now)
            if key is not None:
                expires = None if self.sticky_ttl is None else now + self.sticky_ttl
                self._bindings[key] = _Binding(stat.proxy, expires)
            return self._use(stat, now)

    def wait_time(self) -> float | None:
        """Seconds until a proxy is usable: 0 if one is now, ``None`` if all are dead."""
        with self._lock:
            return self._wait_time(self._clock())

    def release(self, key: str) -> None:
        """Forget the proxy bound to ``key``; the next acquire picks a new one."""
        with self._lock:
            self._bindings.pop(key, None)

    def bound(self, key: str) -> Proxy | None:
        """The proxy currently bound to ``key``, if the binding is still valid."""
        with self._lock:
            binding = self._bindings.get(key)
            if binding is None:
                return None
            if binding.expires_at is not None and self._clock() >= binding.expires_at:
                return None
            return binding.proxy

    # -- feedback ---------------------------------------------------------

    def report(self, proxy: Proxy, outcome: Outcome | str) -> None:
        """Record what happened when ``proxy`` was used.

        A success resets the proxy's failure streak. An error or a ban starts or
        extends its cooldown and unbinds every sticky key that pointed to it, so
        those keys move to another proxy on their next acquire.
        """
        outcome = Outcome(outcome)
        with self._lock:
            stat = self._stats.get(proxy)
            if stat is None:
                return
            if outcome is Outcome.SUCCESS:
                stat.successes += 1
                stat.consecutive_failures = 0
                stat.cooldown_until = 0.0
                return
            if outcome is Outcome.BAN:
                stat.bans += 1
            else:
                stat.errors += 1
            stat.consecutive_failures += 1
            self._unbind(proxy)
            if self.max_failures is not None and stat.consecutive_failures >= self.max_failures:
                stat.dead = True
                return
            delay = min(self.cooldown * 2 ** (stat.consecutive_failures - 1), self.max_cooldown)
            delay *= 1 + self._rng.random() * self.jitter
            stat.cooldown_until = self._clock() + delay

    def success(self, proxy: Proxy) -> None:
        self.report(proxy, Outcome.SUCCESS)

    def failure(self, proxy: Proxy, *, ban: bool = False) -> None:
        self.report(proxy, Outcome.BAN if ban else Outcome.ERROR)

    # -- inspection -------------------------------------------------------

    def stats(self) -> list[ProxyStats]:
        """A snapshot of every proxy's counters, in pool order."""
        with self._lock:
            return [self._stats[p].copy() for p in self._order]

    def summary(self) -> dict[str, int]:
        """Counts of proxies by state: ``total``, ``available``, ``cooling``, ``dead``."""
        with self._lock:
            now = self._clock()
            dead = sum(1 for s in self._stats.values() if s.dead)
            available = sum(1 for s in self._stats.values() if self._usable(s, now))
            return {
                "total": len(self._stats),
                "available": available,
                "cooling": len(self._stats) - available - dead,
                "dead": dead,
                "sticky_keys": len(self._bindings),
            }

    # -- internals (lock held) --------------------------------------------

    def _usable(self, stat: ProxyStats, now: float) -> bool:
        return not stat.dead and stat.cooldown_until <= now

    def _use(self, stat: ProxyStats, now: float) -> Proxy:
        stat.uses += 1
        stat.last_used = now
        return stat.proxy

    def _pick(self, now: float) -> ProxyStats:
        usable = [self._stats[p] for p in self._order if self._usable(self._stats[p], now)]
        if not usable:
            wait = self._wait_time(now)
            if wait is None:
                raise NoProxyAvailable("every proxy in the pool is dead", None)
            raise NoProxyAvailable(f"every proxy is cooling down; next one in {wait:.1f}s", wait)
        if self.strategy is Strategy.RANDOM:
            return self._rng.choice(usable)
        if self.strategy is Strategy.LEAST_USED:
            return min(usable, key=lambda s: (s.uses, s.last_used or 0.0))
        size = len(self._order)
        for step in range(size):
            proxy = self._order[(self._cursor + step) % size]
            stat = self._stats[proxy]
            if self._usable(stat, now):
                self._cursor = (self._cursor + step + 1) % size
                return stat
        raise AssertionError("unreachable: a usable proxy exists")

    def _wait_time(self, now: float) -> float | None:
        alive = [s for s in self._stats.values() if not s.dead]
        if not alive:
            return None
        return max(0.0, min(s.cooldown_until for s in alive) - now)

    def _unbind(self, proxy: Proxy) -> None:
        for key in [k for k, b in self._bindings.items() if b.proxy == proxy]:
            del self._bindings[key]
