# Changelog

All notable changes are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-18

### Added

- `ProxyPool`: round robin, random and least-used strategies, sticky keys with a TTL, exponential backoff with jitter, dead proxies after N failures in a row, thread-safe, counters and summary.
- Proxy parsing for `host:port`, `user:pass@host:port`, `host:port:user:pass` and `scheme://` URLs (http, https, socks4, socks4a, socks5, socks5h), with masked output.
- `BanDetector`: status codes and body markers; 407 treated as a proxy error.
- Adapters: `PooledSession` for requests, `PooledClient` and `AsyncPooledClient` for httpx, `ProxyPoolMiddleware` for Scrapy.
- Concurrent health checks that report to the pool.
- QuanticData source: list plans and generate proxy strings through the documented API.

[Unreleased]: https://github.com/quanticdata-io/proxy-pool/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/quanticdata-io/proxy-pool/releases/tag/v0.1.0
