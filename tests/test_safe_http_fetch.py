import asyncio
import ipaddress
import socket

import pytest

from plugins.safe_http_fetch import (
    ALLOWED_PORTS,
    MAX_REDIRECTS,
    MAX_RESPONSE_BYTES,
    SafeFetchResponse,
    SafeFetchError,
    ValidatingResolver,
    _fetch_public_url,
    is_public_address,
    validate_public_url,
)


def address_info(address: str, port: int = 80):
    parsed = ipaddress.ip_address(address)
    family = socket.AF_INET6 if parsed.version == 6 else socket.AF_INET
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://10.0.0.1/",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.100.100.200/latest/meta-data/",
        "http://[fc00::1]/",
        "http://[fe80::1]/",
    ],
)
def test_rejects_non_public_literal_addresses(url: str) -> None:
    with pytest.raises(SafeFetchError):
        validate_public_url(url)


@pytest.mark.parametrize("url", ["ftp://example.com/", "file:///etc/passwd", "gopher://example.com/", "data:text/plain,hello"])
def test_rejects_non_http_schemes(url: str) -> None:
    with pytest.raises(SafeFetchError):
        validate_public_url(url)


def test_allows_only_centralized_http_and_https_ports() -> None:
    assert ALLOWED_PORTS == {"http": frozenset({80}), "https": frozenset({443})}
    assert validate_public_url("http://93.184.216.34/").port == 80
    assert validate_public_url("https://example.com/").port == 443
    with pytest.raises(SafeFetchError):
        validate_public_url("http://example.com:8080/")


def test_public_address_policy_rejects_shared_and_reserved_ranges() -> None:
    assert is_public_address(ipaddress.ip_address("8.8.8.8"))
    assert is_public_address(ipaddress.ip_address("2606:4700:4700::1111"))
    assert not is_public_address(ipaddress.ip_address("100.64.0.1"))
    assert not is_public_address(ipaddress.ip_address("100.100.100.200"))
    assert not is_public_address(ipaddress.ip_address("64:ff9b::c0a8:101"))


def test_dns_failure_and_empty_results_fail_closed() -> None:
    async def failed_lookup(host: str, port: int, family: int):
        raise SafeFetchError("DNS 解析失败。")

    async def empty_lookup(host: str, port: int, family: int):
        return []

    with pytest.raises(SafeFetchError):
        asyncio.run(ValidatingResolver(lookup=failed_lookup).resolve("example.com", 80))
    with pytest.raises(SafeFetchError):
        asyncio.run(ValidatingResolver(lookup=empty_lookup).resolve("example.com", 80))


def test_dns_rejects_private_or_mixed_answers() -> None:
    async def private_lookup(host: str, port: int, family: int):
        return [address_info("127.0.0.1", port)]

    async def mixed_lookup(host: str, port: int, family: int):
        return [address_info("8.8.8.8", port), address_info("10.0.0.1", port)]

    with pytest.raises(SafeFetchError):
        asyncio.run(ValidatingResolver(lookup=private_lookup).resolve("localhost", 80))
    with pytest.raises(SafeFetchError):
        asyncio.run(ValidatingResolver(lookup=mixed_lookup).resolve("example.com", 80))


async def run_local_response(
    response_bytes: bytes | list[bytes],
    operation,
):
    connection_targets: list[tuple[str, int]] = []
    responses = list(response_bytes) if isinstance(response_bytes, list) else [response_bytes]

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connection_targets.append(writer.get_extra_info("sockname"))
        await reader.readuntil(b"\r\n\r\n")
        writer.write(responses.pop(0))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        result = await operation(port)
        return result, connection_targets
    finally:
        server.close()
        await server.wait_closed()


def local_test_resolver(calls: list[str]) -> ValidatingResolver:
    async def lookup(host: str, port: int, family: int):
        calls.append(host)
        if len(calls) > 1:
            return [address_info("127.0.0.2", port)]
        return [address_info("127.0.0.1", port)]

    return ValidatingResolver(
        lookup=lookup,
        address_validator=lambda address: address == ipaddress.ip_address("127.0.0.1"),
    )


def test_resolver_verified_ip_is_actual_tcp_target_and_not_reresolved(monkeypatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    calls: list[str] = []
    resolver = local_test_resolver(calls)
    body = b"<title>ok</title>public body"
    response = (
        b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
        + str(len(body)).encode("ascii")
        + b"\r\nConnection: close\r\n\r\n"
        + body
    )

    async def operation(port: int):
        return await _fetch_public_url(
            f"http://public-fetch.invalid:{port}/page",
            timeout_seconds=3,
            max_bytes=MAX_RESPONSE_BYTES,
            max_redirects=MAX_REDIRECTS,
            resolver=resolver,
            allowed_ports={"http": frozenset({port}), "https": frozenset({443})},
        )

    result, targets = asyncio.run(run_local_response(response, operation))

    assert calls == ["public-fetch.invalid"]
    assert targets and targets[0][0] == "127.0.0.1"
    assert result.body == b"<title>ok</title>public body"
    assert result.content_type == "text/html"


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "ftp://example.com/file",
        "file:///etc/passwd",
        "gopher://example.com/",
    ],
)
def test_redirect_to_internal_or_non_http_target_is_rejected(location: str) -> None:
    calls: list[str] = []
    resolver = local_test_resolver(calls)
    response = (
        "HTTP/1.1 302 Found\r\n"
        f"Location: {location}\r\n"
        "Content-Length: 0\r\nConnection: close\r\n\r\n"
    ).encode("ascii")

    async def operation(port: int):
        return await _fetch_public_url(
            f"http://redirect-source.invalid:{port}/",
            timeout_seconds=3,
            max_bytes=MAX_RESPONSE_BYTES,
            max_redirects=MAX_REDIRECTS,
            resolver=resolver,
            allowed_ports={"http": frozenset({port, 80}), "https": frozenset({443})},
        )

    with pytest.raises(SafeFetchError):
        asyncio.run(run_local_response(response, operation))


def test_oversized_response_is_rejected() -> None:
    calls: list[str] = []
    resolver = local_test_resolver(calls)
    body = b"x" * 65
    response = (
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 65\r\nConnection: close\r\n\r\n"
        + body
    )

    async def operation(port: int):
        return await _fetch_public_url(
            f"http://large-response.invalid:{port}/",
            timeout_seconds=3,
            max_bytes=64,
            max_redirects=MAX_REDIRECTS,
            resolver=resolver,
            allowed_ports={"http": frozenset({port}), "https": frozenset({443})},
        )

    with pytest.raises(SafeFetchError, match="大小限制"):
        asyncio.run(run_local_response(response, operation))


def test_streaming_response_without_content_length_is_limited() -> None:
    calls: list[str] = []
    resolver = local_test_resolver(calls)
    response = b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n" + (b"x" * 65)

    async def operation(port: int):
        return await _fetch_public_url(
            f"http://streaming-large.invalid:{port}/",
            timeout_seconds=3,
            max_bytes=64,
            max_redirects=MAX_REDIRECTS,
            resolver=resolver,
            allowed_ports={"http": frozenset({port}), "https": frozenset({443})},
        )

    with pytest.raises(SafeFetchError, match="大小限制"):
        asyncio.run(run_local_response(response, operation))


def test_public_redirect_is_resolved_and_connected_again() -> None:
    calls: list[str] = []

    async def lookup(host: str, port: int, family: int):
        calls.append(host)
        return [address_info("127.0.0.1", port)]

    resolver = ValidatingResolver(
        lookup=lookup,
        address_validator=lambda address: address == ipaddress.ip_address("127.0.0.1"),
    )
    first = b"HTTP/1.1 302 Found\r\nLocation: /final\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    body = b"redirected"
    second = (
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: "
        + str(len(body)).encode("ascii")
        + b"\r\nConnection: close\r\n\r\n"
        + body
    )

    async def operation(port: int):
        return await _fetch_public_url(
            f"http://public-redirect.invalid:{port}/start",
            timeout_seconds=3,
            max_bytes=MAX_RESPONSE_BYTES,
            max_redirects=MAX_REDIRECTS,
            resolver=resolver,
            allowed_ports={"http": frozenset({port}), "https": frozenset({443})},
        )

    result, targets = asyncio.run(run_local_response([first, second], operation))

    assert calls == ["public-redirect.invalid", "public-redirect.invalid"]
    assert len(targets) == 2
    assert result.body == body
    assert result.final_url.endswith("/final")


def test_default_response_limit_remains_two_mebibytes() -> None:
    assert MAX_RESPONSE_BYTES == 2 * 1024 * 1024


def test_html_extraction_behavior_remains_in_ai_chat(monkeypatch) -> None:
    import nonebot

    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()
    from plugins import ai_chat

    async def fake_fetch(url: str, *, timeout_seconds: int, headers):
        return SafeFetchResponse(
            url=url,
            final_url=url,
            content_type="text/html; charset=utf-8",
            body=b"<html><title>Example</title><script>bad()</script><body>Hello world</body></html>",
        )

    monkeypatch.setattr(ai_chat, "fetch_public_url", fake_fetch)
    result = asyncio.run(ai_chat.fetch_url_for_agent("https://example.com/"))

    assert result["title"] == "Example"
    assert "Hello world" in result["content"]
    assert "bad()" not in result["content"]
