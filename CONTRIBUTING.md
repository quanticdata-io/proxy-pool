# Contributing

Thanks for helping improve proxy-pool.

## Setup

```bash
git clone https://github.com/quanticdata-io/proxy-pool
cd proxy-pool
uv venv && uv pip install -e ".[all,socks]" --group dev
```

Plain pip works too: `pip install -e ".[all,socks]" pytest pytest-cov mypy ruff types-requests`.

## Checks

CI runs these on Python 3.10 to 3.13, on Ubuntu and Windows:

```bash
ruff check .
ruff format --check .
mypy
pytest
```

## Ground rules

- Tests never use the network or a real proxy. Use the fake proxies and the fake API in `tests/conftest.py` (all on 127.0.0.1).
- The top-level package (`proxy_pool`) and `proxy_pool.quanticdata` stay free of third-party dependencies. Adapters import their library in their own module only.
- Never log or print a password: use `Proxy.masked`.
- A request must never leave from the user's own IP because the pool ran out: fail the request instead.
- User-facing changes need a CHANGELOG entry under "Unreleased".

## Releases

Bump the version in `pyproject.toml` and `src/proxy_pool/_version.py`, update the CHANGELOG and tag `vX.Y.Z`.
