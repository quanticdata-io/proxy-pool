"""Fill a pool with QuanticData sticky sessions and keep one per account.

export QUANTICDATA_API_KEY=qd_live_...       # dashboard, API keys
export QUANTICDATA_ORDER_ID=ord_...           # see client.plans()
python examples/quanticdata_sticky.py
"""

from __future__ import annotations

import os

from proxy_pool import ProxyPool
from proxy_pool.quanticdata import QuanticDataClient
from proxy_pool.requests_adapter import PooledSession

ECHO = "https://api.quanticdata.io/v1/public/ip-echo"


def main() -> None:
    client = QuanticDataClient()
    order_id = os.environ.get("QUANTICDATA_ORDER_ID")
    if not order_id:
        for plan in client.plans():
            print(f"{plan.order_id}  {plan.plan_type:<20} {plan.plan_name}")
        raise SystemExit("set QUANTICDATA_ORDER_ID to one of the order ids above")

    sessions = client.generate(order_id, quantity=5, country="us", rotation="sticky", session_time=10)
    pool = ProxyPool(sessions, sticky_ttl=9 * 60)  # move keys before the 10-minute session ends
    with PooledSession(pool) as session:
        for account in ("account-a", "account-b", "account-a"):
            ip = session.get(ECHO, session_key=account, timeout=30).json()["payload"]["ip"]
            print(account, ip)  # account-a shows the same IP both times


if __name__ == "__main__":
    main()
