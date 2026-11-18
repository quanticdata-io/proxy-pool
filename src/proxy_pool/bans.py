"""Decide whether a response means "this proxy is blocked here"."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

DEFAULT_BAN_STATUSES = frozenset({403, 429})
"""Statuses treated as a block by default. 503 is left out because many sites
answer it for plain overload; add it when your target uses it for blocks."""

PROXY_AUTH_STATUS = 407
"""407 comes from the proxy itself (bad or expired credentials), never from the
target, so it always counts as a proxy error rather than a ban."""


@dataclass(frozen=True)
class BanDetector:
    """Rules that classify a response as a block.

    ``statuses``: HTTP statuses that mean blocked (default 403 and 429).
    ``markers``: case-insensitive substrings of the body that mean blocked even
    on a 200, for example a captcha page title. Only the first ``scan_bytes`` of
    the body are searched.
    """

    statuses: frozenset[int] = DEFAULT_BAN_STATUSES
    markers: tuple[str, ...] = field(default_factory=tuple)
    scan_bytes: int = 65536

    @classmethod
    def build(cls, statuses: Iterable[int] | None = None, markers: Iterable[str] = ()) -> BanDetector:
        return cls(
            statuses=frozenset(statuses) if statuses is not None else DEFAULT_BAN_STATUSES,
            markers=tuple(m.lower() for m in markers if m),
        )

    def is_ban(self, status: int, body: bytes | str | None = None) -> bool:
        if status == PROXY_AUTH_STATUS:
            return False
        if status in self.statuses:
            return True
        if not self.markers or body is None:
            return False
        if isinstance(body, bytes):
            text = body[: self.scan_bytes].decode("utf-8", "replace").lower()
        else:
            text = body[: self.scan_bytes].lower()
        return any(marker.lower() in text for marker in self.markers)

    @staticmethod
    def is_proxy_error(status: int) -> bool:
        """True when the status was produced by the proxy, not the target."""
        return status == PROXY_AUTH_STATUS
