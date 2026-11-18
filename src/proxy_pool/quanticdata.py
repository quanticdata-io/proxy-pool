"""Fill a pool from a QuanticData proxy plan.

Uses two documented, free endpoints of https://quanticdata.io/docs/:
``GET /public/proxies`` (list plans) and ``POST /public/proxies/generate``
(proxy strings with targeting and session already applied). The strings are
used exactly as the API returns them: usernames are never edited here.

Only the standard library is used, so this works without optional extras.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .proxy import Proxy, parse_proxy

DEFAULT_BASE_URL = "https://api.quanticdata.io/v1"
ENV_API_KEY = "QUANTICDATA_API_KEY"
ENV_API_BASE = "QUANTICDATA_API_BASE"

ROTATIONS = ("rotating", "sticky", "static")
PROTOCOLS = ("http", "socks5")


class QuanticDataError(RuntimeError):
    """The API answered with an error envelope or a non-2xx status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"QuanticData API: {message} (HTTP {status})")
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Plan:
    order_id: str
    plan_type: str
    plan_name: str
    is_active: bool
    bandwidth_left_gb: float | None


class QuanticDataClient:
    """Minimal client for the two proxy endpoints.

    ``api_key`` defaults to ``$QUANTICDATA_API_KEY``; ``base_url`` to
    ``$QUANTICDATA_API_BASE`` or ``https://api.quanticdata.io/v1``.
    """

    def __init__(
        self, api_key: str | None = None, base_url: str | None = None, timeout: float = 60.0
    ) -> None:
        key = (api_key or os.environ.get(ENV_API_KEY, "")).strip()
        if not key:
            raise ValueError(f"{ENV_API_KEY} is not set: create an API key in the QuanticData dashboard")
        self.api_key = key
        self.base_url = (base_url or os.environ.get(ENV_API_BASE) or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "proxy-pool (+https://github.com/quanticdata-io/proxy-pool)",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        try:
            envelope = json.loads(raw.decode("utf-8") or "{}")
        except ValueError as exc:
            raise QuanticDataError(status, "the answer is not JSON") from exc
        if status >= 300 or envelope.get("type") != "response":
            raise QuanticDataError(status, str(envelope.get("message") or "unknown error"))
        return envelope.get("payload")

    def plans(self, plan_type: str | None = None, active_only: bool = True) -> list[Plan]:
        """Proxy plans on the account (``GET /public/proxies``)."""
        query = "?limit=50"
        if plan_type:
            query += f"&planType={plan_type}"
        if active_only:
            query += "&active=true"
        payload = self._call("GET", "/public/proxies" + query) or {}
        return [
            Plan(
                order_id=str(item.get("orderId", "")),
                plan_type=str(item.get("planType", "")),
                plan_name=str(item.get("planName", "")),
                is_active=bool(item.get("isActive", False)),
                bandwidth_left_gb=item.get("bandwidthLeftGB"),
            )
            for item in payload.get("proxies", [])
        ]

    def generate(
        self,
        order_id: str,
        *,
        quantity: int = 10,
        protocol: str = "http",
        country: str | None = None,
        state: str | None = None,
        city: str | None = None,
        isp: str | None = None,
        asn: str | None = None,
        rotation: str = "rotating",
        session_time: int | None = None,
        super_sticky: bool = False,
        gateway: str | None = None,
    ) -> list[Proxy]:
        """Proxy strings for one plan (``POST /public/proxies/generate``).

        ``rotation="rotating"`` gives strings that change exit IP on every
        request; ``"sticky"`` keeps one IP per string for ``session_time``
        minutes. Which targeting fields apply depends on the plan type (see the
        API reference); fields a plan does not support are ignored by the API.
        """
        if not order_id.strip():
            raise ValueError("order_id is required (see QuanticDataClient.plans())")
        if not 1 <= quantity <= 10000:
            raise ValueError("quantity must be between 1 and 10000")
        if protocol not in PROTOCOLS:
            raise ValueError(f"protocol must be one of {PROTOCOLS}")
        if rotation not in ROTATIONS:
            raise ValueError(f"rotation must be one of {ROTATIONS}")
        body: dict[str, Any] = {
            "orderId": order_id,
            "quantity": quantity,
            "protocol": protocol,
            "format": f"{protocol}://user:pass@host:port",
            "rotation": rotation,
        }
        optional = {
            "country": country,
            "state": state,
            "city": city,
            "isp": isp,
            "asn": asn,
            "sessionTime": session_time,
            "gateway": gateway,
        }
        body.update({k: v for k, v in optional.items() if v is not None})
        if super_sticky:
            body["superSticky"] = True
        payload = self._call("POST", "/public/proxies/generate", body) or {}
        lines = payload.get("proxies") or []
        return [parse_proxy(str(line), protocol) for line in lines]


def from_quanticdata(order_id: str, **kwargs: Any) -> list[Proxy]:
    """Shortcut: ``QuanticDataClient().generate(order_id, **kwargs)``."""
    client_args = {k: kwargs.pop(k) for k in ("api_key", "base_url") if k in kwargs}
    return QuanticDataClient(**client_args).generate(order_id, **kwargs)
