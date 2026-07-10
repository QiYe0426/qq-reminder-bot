from __future__ import annotations

import asyncio
import ipaddress
import socket
import urllib.parse
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

import aiohttp
from aiohttp.abc import AbstractResolver


ALLOWED_PORTS: Mapping[str, frozenset[int]] = {
    "http": frozenset({80}),
    "https": frozenset({443}),
}
DEFAULT_PORTS: Mapping[str, int] = {"http": 80, "https": 443}
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_URL_LENGTH = 4096
READ_CHUNK_BYTES = 64 * 1024

AddressInfo = tuple[int, int, int, str, tuple[object, ...]]
AddressLookup = Callable[[str, int, int], Awaitable[list[AddressInfo]]]
AddressValidator = Callable[[ipaddress.IPv4Address | ipaddress.IPv6Address], bool]


class SafeFetchError(RuntimeError):
    """Raised when a URL cannot be fetched within the public-network boundary."""


@dataclass(frozen=True)
class ValidatedURL:
    url: str
    scheme: str
    hostname: str
    port: int


@dataclass(frozen=True)
class SafeFetchResponse:
    url: str
    final_url: str
    content_type: str
    body: bytes


def is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None and not is_public_address(mapped):
        return False
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_unspecified
        and not address.is_reserved
    )


def validate_public_url(
    url: str,
    *,
    allowed_ports: Mapping[str, frozenset[int]] = ALLOWED_PORTS,
) -> ValidatedURL:
    raw_url = str(url or "").strip()
    if not raw_url or len(raw_url) > MAX_URL_LENGTH:
        raise SafeFetchError("URL 为空或过长。")
    if "\\" in raw_url or any(ord(char) < 32 or ord(char) == 127 for char in raw_url):
        raise SafeFetchError("URL 包含不允许的字符。")

    try:
        parsed = urllib.parse.urlsplit(raw_url)
        hostname = parsed.hostname
        explicit_port = parsed.port
    except ValueError as exc:
        raise SafeFetchError("URL 格式或端口无效。") from exc

    scheme = parsed.scheme.lower()
    if scheme not in allowed_ports:
        raise SafeFetchError("只允许抓取 http/https 网页。")
    if not hostname:
        raise SafeFetchError("URL 缺少主机名。")
    if parsed.username or parsed.password:
        raise SafeFetchError("URL 不能包含用户名或密码。")

    hostname = hostname.rstrip(".")
    if not hostname or "%" in hostname:
        raise SafeFetchError("URL 主机名无效。")
    try:
        literal_address = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            hostname = hostname.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise SafeFetchError("URL 主机名无效。") from exc
    else:
        if not is_public_address(literal_address):
            raise SafeFetchError("出于安全原因，不能抓取非公网地址。")
        hostname = literal_address.compressed

    port = explicit_port or DEFAULT_PORTS[scheme]
    if port not in allowed_ports[scheme]:
        raise SafeFetchError("URL 端口不在允许范围内。")

    host_for_url = f"[{hostname}]" if ":" in hostname else hostname
    netloc = host_for_url if explicit_port is None else f"{host_for_url}:{port}"
    path = parsed.path or "/"
    normalized = urllib.parse.urlunsplit((scheme, netloc, path, parsed.query, ""))
    return ValidatedURL(url=normalized, scheme=scheme, hostname=hostname, port=port)


async def default_address_lookup(hostname: str, port: int, family: int) -> list[AddressInfo]:
    loop = asyncio.get_running_loop()
    try:
        return await loop.getaddrinfo(
            hostname,
            port,
            family=family,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except OSError as exc:
        raise SafeFetchError("DNS 解析失败。") from exc


class ValidatingResolver(AbstractResolver):
    """Resolve once, validate every answer, and return only numeric addresses."""

    def __init__(
        self,
        *,
        lookup: AddressLookup | None = None,
        address_validator: AddressValidator = is_public_address,
    ) -> None:
        self._lookup = lookup or default_address_lookup
        self._address_validator = address_validator

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: int = socket.AF_UNSPEC,
    ) -> list[dict[str, object]]:
        try:
            literal_address = ipaddress.ip_address(host)
        except ValueError:
            address_infos = await self._lookup(host, port, family)
        else:
            address_infos = [
                (
                    socket.AF_INET6 if literal_address.version == 6 else socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    (literal_address.compressed, port),
                )
            ]

        if not address_infos:
            raise SafeFetchError("DNS 没有返回可用地址。")

        resolved: list[dict[str, object]] = []
        seen: set[tuple[int, str]] = set()
        for family_value, _, proto, _, sockaddr in address_infos:
            if not sockaddr:
                raise SafeFetchError("DNS 返回了无效地址。")
            try:
                address = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
            except ValueError as exc:
                raise SafeFetchError("DNS 返回了无效地址。") from exc
            if not self._address_validator(address):
                raise SafeFetchError("DNS 解析到了非公网地址。")
            key = (family_value, address.compressed)
            if key in seen:
                continue
            seen.add(key)
            resolved.append(
                {
                    "hostname": host,
                    "host": address.compressed,
                    "port": port,
                    "family": family_value,
                    "proto": proto or socket.IPPROTO_TCP,
                    "flags": socket.AI_NUMERICHOST,
                }
            )

        if not resolved:
            raise SafeFetchError("DNS 没有返回可用公网地址。")
        return resolved

    async def close(self) -> None:
        return None


async def _read_limited_body(response: aiohttp.ClientResponse, max_bytes: int) -> bytes:
    content_length = response.content_length
    if content_length is not None and content_length > max_bytes:
        raise SafeFetchError("网页响应超过大小限制。")

    body = bytearray()
    async for chunk in response.content.iter_chunked(READ_CHUNK_BYTES):
        body.extend(chunk)
        if len(body) > max_bytes:
            raise SafeFetchError("网页响应超过大小限制。")
    return bytes(body)


async def fetch_public_url(
    url: str,
    *,
    timeout_seconds: int,
    max_bytes: int = MAX_RESPONSE_BYTES,
    max_redirects: int = MAX_REDIRECTS,
    headers: Mapping[str, str] | None = None,
) -> SafeFetchResponse:
    return await _fetch_public_url(
        url,
        timeout_seconds=timeout_seconds,
        max_bytes=max_bytes,
        max_redirects=max_redirects,
        resolver=ValidatingResolver(),
        allowed_ports=ALLOWED_PORTS,
        headers=headers,
    )


async def _fetch_public_url(
    url: str,
    *,
    timeout_seconds: int,
    max_bytes: int,
    max_redirects: int,
    resolver: AbstractResolver,
    allowed_ports: Mapping[str, frozenset[int]],
    headers: Mapping[str, str] | None = None,
) -> SafeFetchResponse:
    # Custom ports exist only for isolated connector tests; production uses ALLOWED_PORTS.
    initial = validate_public_url(url, allowed_ports=allowed_ports)

    connector = aiohttp.TCPConnector(
        resolver=resolver,
        use_dns_cache=False,
        force_close=True,
        limit=1,
    )
    timeout = aiohttp.ClientTimeout(total=max(1, min(int(timeout_seconds), 60)))
    request_headers = dict(headers or {})
    visited: set[str] = set()
    current = initial

    async with aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        trust_env=False,
        cookie_jar=aiohttp.DummyCookieJar(),
    ) as session:
        for redirect_count in range(max_redirects + 1):
            if current.url in visited:
                raise SafeFetchError("网页重定向形成循环。")
            visited.add(current.url)

            try:
                async with session.get(
                    current.url,
                    headers=request_headers,
                    allow_redirects=False,
                    proxy=None,
                ) as response:
                    if response.status in REDIRECT_STATUSES:
                        if redirect_count >= max_redirects:
                            raise SafeFetchError("网页重定向次数过多。")
                        location = response.headers.get("Location", "").strip()
                        if not location:
                            raise SafeFetchError("网页重定向缺少目标地址。")
                        redirect_url = urllib.parse.urljoin(current.url, location)
                        current = validate_public_url(redirect_url, allowed_ports=allowed_ports)
                        continue
                    if response.status >= 400:
                        raise SafeFetchError(f"网页返回 HTTP {response.status}。")
                    body = await _read_limited_body(response, max_bytes)
                    return SafeFetchResponse(
                        url=initial.url,
                        final_url=current.url,
                        content_type=response.headers.get("Content-Type", ""),
                        body=body,
                    )
            except SafeFetchError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise SafeFetchError("网页抓取失败。") from exc

    raise SafeFetchError("网页抓取失败。")
