# proxy-pool

[![CI](https://github.com/quanticdata-io/proxy-pool/actions/workflows/ci.yml/badge.svg)](https://github.com/quanticdata-io/proxy-pool/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A proxy pool for Python scrapers. It hands out proxies (round robin, random or least used), keeps a sticky proxy per key (a domain, an account, a browser profile), rests proxies that fail with exponential backoff, drops the ones that keep failing, and retries a blocked request through another proxy.

It ships with ready adapters for **requests**, **httpx** (sync and async) and **Scrapy** (a downloader middleware), and works with any HTTP, HTTPS or SOCKS proxy list from any provider. The core has no dependencies.

```python
from proxy_pool import ProxyPool
from proxy_pool.requests_adapter import PooledSession

pool = ProxyPool(open("proxies.txt"))
with PooledSession(pool) as session:
    r = session.get("https://example.com/")  # blocked (403/429) or failed? retried through another proxy
```

## Install

Not on PyPI yet: install from source.

```bash
pip install "proxy-pool[requests] @ git+https://github.com/quanticdata-io/proxy-pool"
pip install "proxy-pool[httpx] @ git+https://github.com/quanticdata-io/proxy-pool"
pip install "proxy-pool[scrapy] @ git+https://github.com/quanticdata-io/proxy-pool"
pip install "proxy-pool[all,socks] @ git+https://github.com/quanticdata-io/proxy-pool"   # everything, SOCKS too
```

Python 3.10 or later. Extras: `requests`, `httpx`, `scrapy`, `socks` (adds `requests[socks]` and `httpx[socks]`), `all`.

## Two ways to rotate, and which one you have

Most residential and mobile providers, QuanticData included, give you one of two things. The pool handles both, but they behave differently:

| You have | What it does | How to use it here |
|---|---|---|
| **A rotating gateway**: one host:port, new exit IP on every request | Rotation happens at the provider | A pool of one entry still gives you retries on blocks and backoff on errors. Add several entries (different countries, or several plans) to spread load |
| **Sticky session strings**: each string keeps one exit IP for some minutes | One string = one session = one IP | Put many strings in the pool and use `key=` so each domain or account keeps its own session; a ban moves that key to the next string |

Use sticky strings when a site ties a login, a cart or a captcha answer to the IP; use rotation for independent page fetches.

## The pool

```python
from proxy_pool import ProxyPool, Outcome

pool = ProxyPool(
    ["user:pass@gw.example.net:8080", "socks5://u:p@198.51.100.4:1080", "203.0.113.7:3128"],
    strategy="round_robin",  # or "random", "least_used"
    sticky_ttl=600,  # seconds a key keeps its proxy (None = until it fails)
    cooldown=30,  # first rest after a failure; doubles per failure in a row
    max_cooldown=600,
    max_failures=5,  # failures in a row before a proxy is dead (None = never)
    jitter=0.1,  # spread cooldowns by up to +10%
)

proxy = pool.acquire()  # next proxy by strategy
proxy = pool.acquire(key="shop.test")  # same proxy for this key until it fails or expires
print(proxy.url, proxy.masked)  # "http://user:pass@..." and "http://user:*****@..."

pool.report(proxy, Outcome.SUCCESS)  # or Outcome.ERROR (connection) / Outcome.BAN (blocked)
pool.summary()  # {"total": 3, "available": 2, "cooling": 1, "dead": 0, "sticky_keys": 1}
pool.stats()  # per-proxy counters: uses, successes, errors, bans, cooldown_until, dead
```

When every proxy is resting, `acquire()` raises `NoProxyAvailable` with `retry_after` (seconds; `None` when all are dead). `wait_time()` gives the same number without raising. `revive()` clears dead flags, `add()` and `remove()` change membership at run time. The pool is thread-safe.

Proxy strings are read in these forms (blank lines and `#` comments are skipped in files):

| Form | Example |
|---|---|
| `host:port` | `203.0.113.7:3128` |
| `user:pass@host:port` | `user:pass@gw.example.net:8080` |
| `host:port:user:pass` | `gw.example.net:8080:user:pass` |
| `scheme://[user:pass@]host:port` | `socks5://user:pass@198.51.100.4:1080` |

Schemes: `http`, `https`, `socks4`, `socks4a`, `socks5`, `socks5h`. A password may contain `:` or `@`. Passwords never appear in `str()`, `repr()` or parse errors.

## What counts as a block

`BanDetector` decides. By default a **403 or 429** is a ban. A **407** comes from the proxy itself (wrong or expired credentials), so it counts as a proxy error, never a ban. Add statuses or body markers for your target:

```python
from proxy_pool import BanDetector

detector = BanDetector.build(statuses=[403, 429, 503], markers=["captcha", "access denied"])
```

Markers are matched case-insensitively in the first 64 KB of the body, also on a 200.

## requests

```python
from proxy_pool.requests_adapter import PooledSession

with PooledSession(pool, detector=detector, max_attempts=3) as session:
    session.get("https://example.com/")  # rotates
    session.get("https://example.com/cart", session_key="acct-1")  # sticky for acct-1
```

`PooledSession` is a `requests.Session`. Each try takes a proxy from the pool; connection errors, timeouts, 407 and bans are reported and retried through another proxy. The last response is returned even when it is a ban, so you can look at it. **Only idempotent methods are retried**: a POST is sent once unless you pass `retry_methods`. Do not pass `proxies=` yourself.

## httpx

```python
from proxy_pool.httpx_adapter import AsyncPooledClient, PooledClient

with PooledClient(pool, timeout=20) as client:
    client.get("https://example.com/")

async with AsyncPooledClient(pool, timeout=20) as client:
    await asyncio.gather(*(client.get(u) for u in urls))
```

httpx binds a proxy to a client, so these wrappers keep one `httpx.Client` per proxy, created on first use and closed with the wrapper. Keyword arguments other than the pool options (`detector`, `max_attempts`, `retry_methods`) go to every underlying client.

## Scrapy

```python
# settings.py
DOWNLOADER_MIDDLEWARES = {
    "proxy_pool.scrapy_middleware.ProxyPoolMiddleware": 610,
}
PROXY_POOL_FILE = "proxies.txt"
PROXY_POOL_STICKY = "domain"  # one proxy per host until it fails
```

Priority 610 runs it before Scrapy's `HttpProxyMiddleware` (750), which turns the credentials in the proxy URL into the `Proxy-Authorization` header, and its responses are seen before Scrapy's `RetryMiddleware` (550). A blocked or failed request is rescheduled through another proxy with the stale proxy and auth header removed.

| Setting | Default | Meaning |
|---|---|---|
| `PROXY_POOL_ENABLED` | `True` | Turn the middleware off without removing it |
| `PROXY_POOL_LIST` | `[]` | Proxy strings |
| `PROXY_POOL_FILE` | unset | Path to a proxy list file |
| `PROXY_POOL_QUANTICDATA` | unset | Dict of `generate()` arguments, for example `{"order_id": "ord_...", "country": "us", "rotation": "sticky", "quantity": 50}` (needs `QUANTICDATA_API_KEY`) |
| `PROXY_POOL_DEFAULT_SCHEME` | `http` | Scheme for strings without one |
| `PROXY_POOL_STRATEGY` | `round_robin` | `round_robin`, `random`, `least_used` |
| `PROXY_POOL_STICKY` | `none` | `none` or `domain` |
| `PROXY_POOL_STICKY_TTL` | `600` | Seconds; `0` keeps a binding until the proxy fails |
| `PROXY_POOL_COOLDOWN` / `PROXY_POOL_MAX_COOLDOWN` | `30` / `600` | Backoff in seconds |
| `PROXY_POOL_MAX_FAILURES` | `5` | Failures in a row before a proxy is dead; `0` = never |
| `PROXY_POOL_BAN_STATUSES` | `[403, 429]` | Statuses treated as a block |
| `PROXY_POOL_BAN_MARKERS` | `[]` | Body substrings treated as a block |
| `PROXY_POOL_MAX_RETRIES` | `5` | Retries per request before `IgnoreRequest` |
| `PROXY_POOL_MAX_WAIT` | `60` | Seconds a request may wait for a proxy to leave its cooldown |
| `PROXY_POOL_SLOT_PER_PROXY` | `False` | One download slot per proxy, so `DOWNLOAD_DELAY` and per-slot concurrency apply per proxy instead of per domain |

Per request: `meta={"proxy_session_key": "acct-1"}` pins a key, `meta={"proxy_pool_skip": True}` sends the request without a pool proxy, and a request that already has `meta["proxy"]` is left alone. Counters appear in the crawl stats under `proxy_pool/`. When every proxy is dead the request is dropped with `IgnoreRequest`; it is never sent from your own IP.

## Health checks

```python
from proxy_pool.health import check_pool_sync

results = check_pool_sync(pool, url="https://api.quanticdata.io/v1/public/ip-echo", concurrency=20)
print(pool.summary())
```

One GET per proxy; the result of each is reported to the pool, so dead proxies are rested before your crawl starts. The default URL is the free QuanticData IP echo, which needs no account; any URL that answers 2xx works. For a full audit of a list (latency percentiles, exit country, rotation, anonymity, CSV reports), use [proxy-checker](https://github.com/quanticdata-io/proxy-checker).

## QuanticData proxies

With a QuanticData residential, mobile, ISP or datacenter plan, the pool can be filled with fresh strings from your plan through the documented [`POST /public/proxies/generate`](https://quanticdata.io/docs/) endpoint (free to call; traffic is billed by the plan as usual). The strings come back with country, city, carrier and session already applied, and are used as returned.

```python
import os
from proxy_pool import ProxyPool
from proxy_pool.quanticdata import QuanticDataClient

client = QuanticDataClient()  # reads QUANTICDATA_API_KEY
for plan in client.plans():
    print(plan.order_id, plan.plan_type, plan.plan_name)

# 50 sticky sessions in the US, 10 minutes each: one IP per string
sessions = client.generate(
    os.environ["QUANTICDATA_ORDER_ID"], quantity=50, country="us", rotation="sticky", session_time=10
)
pool = ProxyPool(sessions, sticky_ttl=9 * 60)  # let keys move before the session ends
```

`generate()` takes `quantity` (1 to 10000), `protocol` (`http`, `socks5`), `country`, `state`, `city`, `isp`, `asn`, `rotation` (`rotating`, `sticky`, `static`), `session_time` (minutes), `super_sticky` and `gateway`. Which apply depends on the plan type, as listed in the [API reference](https://quanticdata.io/docs/); `QUANTICDATA_API_BASE` overrides the API host. Plans and prices: [quanticdata.io/pricing](https://quanticdata.io/pricing/).

## Limits

- The pool is in memory and per process. Several worker processes each keep their own counters; nothing is shared or persisted.
- A ban is a guess from the status and body. Sites that answer 200 with an empty or fake page need markers you choose.
- With a rotating gateway, the pool sees one entry: it cannot tell which exit IP was banned, only that the request should be retried.
- Retrying through another proxy does not make a request safe to repeat: keep non-idempotent methods out of `retry_methods` unless your target deduplicates them.
- HTTPS targets are tunnelled (CONNECT); the proxy cannot see or change them, and the pool never decrypts traffic.

## Examples

[`examples/`](examples/) has a requests script, an async httpx crawler, a Scrapy spider and the QuanticData sticky-session setup. Each reads credentials from environment variables.

## Development

```bash
uv venv && uv pip install -e ".[all,socks]" --group dev
ruff check . && ruff format --check . && mypy && pytest
```

Tests run against in-process fake proxies and a fake API on 127.0.0.1; they never use the network. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
