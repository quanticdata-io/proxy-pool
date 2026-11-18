## What and why

<!-- What does this change and which problem does it solve? Link the issue if there is one. -->

## Checklist

- [ ] `ruff check .`, `ruff format --check .`, `mypy` and `pytest` pass
- [ ] New behaviour has tests (fake proxies on 127.0.0.1 only, no network)
- [ ] No password can reach a log, an error or an output
- [ ] `CHANGELOG.md` updated under "Unreleased"
