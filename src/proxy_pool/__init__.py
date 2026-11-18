"""Rotating and sticky proxy pool with health checks, ban detection and backoff.

The core (this package's top level) has no dependencies. Adapters:

- ``proxy_pool.requests_adapter.PooledSession`` (needs ``requests``)
- ``proxy_pool.httpx_adapter.PooledClient`` / ``AsyncPooledClient`` (needs ``httpx``)
- ``proxy_pool.scrapy_middleware.ProxyPoolMiddleware`` (needs ``scrapy``)
- ``proxy_pool.health`` (needs ``httpx``)
- ``proxy_pool.quanticdata`` (standard library only)
"""

from ._version import __version__
from .bans import DEFAULT_BAN_STATUSES, BanDetector
from .pool import NoProxyAvailable, Outcome, ProxyPool, ProxyStats, Strategy
from .proxy import Proxy, ProxyParseError, load_file, parse_lines, parse_proxy

__all__ = [
    "DEFAULT_BAN_STATUSES",
    "BanDetector",
    "NoProxyAvailable",
    "Outcome",
    "Proxy",
    "ProxyParseError",
    "ProxyPool",
    "ProxyStats",
    "Strategy",
    "__version__",
    "load_file",
    "parse_lines",
    "parse_proxy",
]
