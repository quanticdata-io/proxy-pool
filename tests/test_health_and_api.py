"""Health checks and the QuanticData source."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from conftest import FakeAPI, FakeProxy
from proxy_pool import ProxyPool, parse_proxy
from proxy_pool.health import check_pool_sync
from proxy_pool.quanticdata import QuanticDataClient, QuanticDataError, from_quanticdata

CHECK_URL = "http://echo.test/ip"


def test_health_check_reports_to_pool(make_proxy: Callable[..., FakeProxy], dead_proxy_url: str) -> None:
    good = make_proxy("good")
    banned = make_proxy("banned", "ban")
    pool = ProxyPool([good.url, dead_proxy_url, banned.url], max_failures=1)
    results = check_pool_sync(pool, url=CHECK_URL, timeout=5)
    assert [r.ok for r in results] == [True, False, False]
    assert results[0].status == 200
    assert results[1].status is None
    assert results[1].error
    assert results[2].status == 403
    assert pool.summary()["dead"] == 2
    assert pool.acquire().port == good.port


def test_generate_sends_documented_body(fake_api: FakeAPI) -> None:
    fake_api.responses["POST /public/proxies/generate"] = (
        200,
        {
            "type": "response",
            "message": "success",
            "payload": {
                "proxies": [
                    "http://user_8c21-country-us-sid-aaa-ttl-10m:p4ss@gw.example.net:8080",
                    "http://user_8c21-country-us-sid-bbb-ttl-10m:p4ss@gw.example.net:8080",
                ]
            },
        },
    )
    client = QuanticDataClient(api_key="qd_test_key", base_url=fake_api.base_url)
    proxies = client.generate("ord_1", quantity=2, country="us", rotation="sticky", session_time=10)
    method, path, body, auth = fake_api.requests[0]
    assert (method, path, auth) == ("POST", "/public/proxies/generate", "Bearer qd_test_key")
    assert body == {
        "orderId": "ord_1",
        "quantity": 2,
        "protocol": "http",
        "format": "http://user:pass@host:port",
        "rotation": "sticky",
        "country": "us",
        "sessionTime": 10,
    }
    assert proxies[0] == parse_proxy("http://user_8c21-country-us-sid-aaa-ttl-10m:p4ss@gw.example.net:8080")
    assert proxies[0].username == "user_8c21-country-us-sid-aaa-ttl-10m"


def test_generate_super_sticky_and_socks(fake_api: FakeAPI) -> None:
    fake_api.responses["POST /public/proxies/generate"] = (
        200,
        {"type": "response", "payload": {"proxies": ["socks5://u:p@gw.example.net:1080"]}},
    )
    proxies = from_quanticdata(
        "ord_1", api_key="k", base_url=fake_api.base_url, protocol="socks5", super_sticky=True, quantity=1
    )
    body = fake_api.requests[0][2]
    assert body["superSticky"] is True
    assert body["format"] == "socks5://user:pass@host:port"
    assert proxies[0].scheme == "socks5"


def test_api_error_envelope(fake_api: FakeAPI) -> None:
    fake_api.responses["POST /public/proxies/generate"] = (
        400,
        {"type": "error", "message": "Proxy has expired"},
    )
    client = QuanticDataClient(api_key="k", base_url=fake_api.base_url)
    with pytest.raises(QuanticDataError, match="Proxy has expired") as info:
        client.generate("ord_1")
    assert info.value.status == 400


def test_plans(fake_api: FakeAPI) -> None:
    fake_api.responses["GET /public/proxies"] = (
        200,
        {
            "type": "response",
            "payload": {
                "proxies": [
                    {"orderId": "ord_1", "planType": "mobile_v2", "planName": "Mobile", "isActive": True,
                     "bandwidthLeftGB": 1.5},
                ]
            },
        },
    )  # fmt: skip
    plans = QuanticDataClient(api_key="k", base_url=fake_api.base_url).plans(plan_type="mobile_v2")
    assert plans[0].order_id == "ord_1"
    assert plans[0].plan_type == "mobile_v2"
    assert "planType=mobile_v2" in fake_api.requests[0][1]


def test_client_needs_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QUANTICDATA_API_KEY", raising=False)
    with pytest.raises(ValueError, match="QUANTICDATA_API_KEY"):
        QuanticDataClient()


@pytest.mark.parametrize("kwargs", [{"quantity": 0}, {"protocol": "ftp"}, {"rotation": "sometimes"}])
def test_generate_validates_input(kwargs: dict[str, object]) -> None:
    client = QuanticDataClient(api_key="k", base_url="http://127.0.0.1:9")
    with pytest.raises(ValueError):  # noqa: PT011
        client.generate("ord_1", **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="order_id"):
        client.generate(" ")
