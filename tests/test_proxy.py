from __future__ import annotations

from pathlib import Path

import pytest

from proxy_pool import Proxy, ProxyParseError, load_file, parse_lines, parse_proxy


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("203.0.113.7:3128", "http://203.0.113.7:3128"),
        ("user:pass@gw.example.net:8080", "http://user:pass@gw.example.net:8080"),
        ("gw.example.net:8080:user:pass", "http://user:pass@gw.example.net:8080"),
        ("socks5://u:p@198.51.100.4:1080", "socks5://u:p@198.51.100.4:1080"),
        ("SOCKS5H://198.51.100.4:1080", "socks5h://198.51.100.4:1080"),
        ("http://[2001:db8::1]:8080", "http://[2001:db8::1]:8080"),
        ("u:p@[2001:db8::1]:8080", "http://u:p@[2001:db8::1]:8080"),
    ],
)
def test_parse_forms(text: str, expected: str) -> None:
    assert parse_proxy(text).url == expected


def test_password_with_colon_and_at_round_trips() -> None:
    proxy = parse_proxy("gw.example.net:8080:user-country-us:pa:ss@1")
    assert proxy.username == "user-country-us"
    assert proxy.password == "pa:ss@1"
    assert parse_proxy(proxy.url) == proxy
    assert "pa%3Ass%401" in proxy.url


def test_percent_encoded_credentials_are_decoded() -> None:
    proxy = parse_proxy("http://us%40er:p%3Ass@gw.example.net:80")
    assert proxy.username == "us@er"
    assert proxy.password == "p:ss"


def test_masked_never_shows_the_password() -> None:
    proxy = parse_proxy("user:secret@gw.example.net:8080")
    assert "secret" not in proxy.masked
    assert "secret" not in str(proxy)
    assert "secret" not in repr(proxy)
    assert proxy.server == "http://gw.example.net:8080"


def test_default_scheme() -> None:
    assert parse_proxy("198.51.100.4:1080", "socks5").scheme == "socks5"
    with pytest.raises(ProxyParseError):
        parse_proxy("198.51.100.4:1080", "ftp")


@pytest.mark.parametrize("bad", ["", "nohost", "ftp://h:1", "h:99999", "http://h:1/path", "a:b:c"])
def test_rejects_bad_input(bad: str) -> None:
    with pytest.raises(ProxyParseError):
        parse_proxy(bad)


def test_errors_do_not_leak_passwords() -> None:
    with pytest.raises(ProxyParseError) as info:
        parse_proxy("user:topsecret@host:notaport")
    assert "topsecret" not in str(info.value)


def test_parse_lines_skips_comments_and_duplicates() -> None:
    proxies = parse_lines(["# list", "", "h1:1", "h2:2", "h1:1", "  h3:3  "])
    assert [p.host for p in proxies] == ["h1", "h2", "h3"]


def test_parse_lines_names_the_bad_line() -> None:
    with pytest.raises(ProxyParseError, match="line 2"):
        parse_lines(["h1:1", "garbage"])


def test_load_file(tmp_path: Path) -> None:
    path = tmp_path / "proxies.txt"
    path.write_text("h1:1\nu:p@h2:2\n", encoding="utf-8")
    assert load_file(str(path)) == [Proxy("http", "h1", 1), Proxy("http", "h2", 2, "u", "p")]
