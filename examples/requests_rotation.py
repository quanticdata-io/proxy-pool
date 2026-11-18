"""Fetch a few pages through a proxy list with requests.

PROXY_FILE=proxies.txt python examples/requests_rotation.py https://example.com/ https://example.org/
"""

from __future__ import annotations

import os
import sys

from proxy_pool import BanDetector, ProxyPool, load_file
from proxy_pool.requests_adapter import PooledSession


def main(urls: list[str]) -> None:
    pool = ProxyPool(load_file(os.environ.get("PROXY_FILE", "proxies.txt")), cooldown=20)
    detector = BanDetector.build(markers=["captcha"])
    with PooledSession(pool, detector=detector, max_attempts=4) as session:
        for url in urls:
            response = session.get(url, timeout=30)
            print(response.status_code, url)
    print(pool.summary())


if __name__ == "__main__":
    main(sys.argv[1:] or ["https://api.quanticdata.io/v1/public/ip-echo"] * 3)
