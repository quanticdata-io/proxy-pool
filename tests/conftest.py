"""In-process test servers: fake forward proxies and a fake QuanticData API.

Nothing here touches the network beyond 127.0.0.1.
"""

from __future__ import annotations

import base64
import json
import socket
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest


@dataclass
class FakeProxy:
    """A forward proxy that answers plain-HTTP requests itself.

    ``mode``: ``ok`` answers 200 with JSON naming the proxy and the requested
    URL; ``ban`` answers 403 like a target that blocks this exit; ``captcha``
    answers 200 with a captcha page. With ``credentials`` set, requests
    without a matching ``Proxy-Authorization`` get 407.
    """

    name: str
    mode: str = "ok"
    credentials: tuple[str, str] | None = None
    hits: list[str] = field(default_factory=list)
    port: int = 0
    server: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        if self.credentials:
            user, password = self.credentials
            return f"http://{user}:{password}@127.0.0.1:{self.port}"
        return f"http://127.0.0.1:{self.port}"


def _handler(proxy: FakeProxy) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            if proxy.credentials:
                user, password = proxy.credentials
                expected = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
                if self.headers.get("Proxy-Authorization") != expected:
                    self._send(407, b'{"error": "proxy auth"}')
                    return
            proxy.hits.append(self.path)
            if proxy.mode == "ban":
                self._send(403, b"<html>Access denied</html>", "text/html")
            elif proxy.mode == "captcha":
                self._send(200, b"<html><title>Please solve this CAPTCHA</title></html>", "text/html")
            else:
                self._send(200, json.dumps({"via": proxy.name, "url": self.path}).encode())

        do_POST = do_GET
        do_HEAD = do_GET

    return Handler


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def make_proxy() -> Iterator[Callable[..., FakeProxy]]:
    started: list[FakeProxy] = []

    def make(name: str, mode: str = "ok", credentials: tuple[str, str] | None = None) -> FakeProxy:
        proxy = FakeProxy(name, mode, credentials)
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(proxy))
        proxy.server = server
        proxy.port = int(server.server_address[1])
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append(proxy)
        return proxy

    yield make
    for proxy in started:
        assert proxy.server is not None
        proxy.server.shutdown()
        proxy.server.server_close()


@pytest.fixture
def dead_proxy_url() -> Iterator[str]:
    """A broken proxy: it accepts each connection and closes it at once.

    (A closed port would do too, but Windows retries refused connections for
    about two seconds, which makes tests slow and timing-dependent.)
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(64)
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    stop.set()
    listener.close()


@dataclass
class FakeAPI:
    base_url: str
    requests: list[tuple[str, str, Any, str | None]]
    responses: dict[str, tuple[int, Any]]


@pytest.fixture
def fake_api() -> Iterator[FakeAPI]:
    """A fake QuanticData API. Set ``responses["POST /public/proxies/generate"]``."""
    seen: list[tuple[str, str, Any, str | None]] = []
    responses: dict[str, tuple[int, Any]] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _handle(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            path = self.path.removeprefix("/v1")
            seen.append((method, path, body, self.headers.get("Authorization")))
            status, payload = responses.get(
                f"{method} {path.split('?')[0]}", (404, {"type": "error", "message": "not found"})
            )
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield FakeAPI(f"http://127.0.0.1:{server.server_address[1]}/v1", seen, responses)
    server.shutdown()
    server.server_close()


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
