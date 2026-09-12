# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Fetching a URL an agent chose, without fetching your own infrastructure.

An agent that can show the caller a picture will sooner or later be handed a
URL by its own model, and that model is steered by whoever is on the call. So
the URL is untrusted input wearing a trusted costume, and a naive ``GET`` of it
is a server-side request forgery: ``169.254.169.254`` is cloud credentials,
``127.0.0.1`` is whatever else you run, and ``10.0.0.0/8`` is the rest of your
network.

:func:`fetch_public_image` is the guarded way to do it. It accepts http and
https only, refuses embedded credentials, and rejects any host that resolves
into private, loopback, link-local, or reserved space.

The subtle half is the rebind: validating a hostname resolves it once, and the
resolution the HTTP client does a moment later can answer differently. The
window is closed by resolving through :class:`_GuardedResolver`, so the address
the socket actually connects to is re-checked against the same rules. One
redirect hop is followed, because image CDNs habitually redirect to the real
asset, and the target is put through the whole guard again rather than trusted
for having come from a host that passed.

This lives in the SDK rather than in a plugin because more than one
plugin needs it, and two copies of a security control drift. They already
had: of the two implementations this replaces, one followed no redirects and
broke on ordinary CDNs, while the other had grown the re-validating hop. The
better one is here, and now every plugin has it.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp import abc as aiohttp_abc

__all__ = ["assert_public_http_url", "fetch_public_image", "is_forbidden_ip"]

#: Ranges that must never be fetched server-side, beyond what
#: :mod:`ipaddress`'s own flags cover.
_FORBIDDEN_V4 = [
    ipaddress.ip_network("0.0.0.0/8"),  # "this" network
    ipaddress.ip_network("10.0.0.0/8"),  # RFC1918
    ipaddress.ip_network("100.64.0.0/10"),  # carrier-grade NAT
    ipaddress.ip_network("127.0.0.0/8"),  # loopback
    ipaddress.ip_network("169.254.0.0/16"),  # link-local, including cloud metadata
    ipaddress.ip_network("172.16.0.0/12"),  # RFC1918
    ipaddress.ip_network("192.0.0.0/24"),  # IETF protocol assignments
    ipaddress.ip_network("192.168.0.0/16"),  # RFC1918
    ipaddress.ip_network("198.18.0.0/15"),  # benchmarking
    ipaddress.ip_network("224.0.0.0/3"),  # multicast, reserved and broadcast
]

#: Resolves a hostname to addresses. Replaceable so tests can drive a rebind
#: without a DNS server.
LookupFn = Callable[[str], Awaitable[list[str]]]

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def _is_forbidden_v4(addr: ipaddress.IPv4Address) -> bool:
    return any(addr in net for net in _FORBIDDEN_V4)


def _is_forbidden_v6(addr: ipaddress.IPv6Address) -> bool:
    # A v4 address wearing a v6 spelling is still that v4 address: ::ffff:10.0.0.1
    # and the NAT64 prefix both have to be judged on what they embed, or the
    # whole v4 table above is one notation away from being bypassed.
    mapped = addr.ipv4_mapped
    if mapped is not None:
        return _is_forbidden_v4(mapped)
    if addr in ipaddress.ip_network("64:ff9b::/96"):
        return _is_forbidden_v4(ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF))
    if addr == ipaddress.IPv6Address("::") or addr.is_loopback:
        return True
    if addr in ipaddress.ip_network("fc00::/7"):
        return True  # unique-local
    return bool(addr.is_link_local)


def is_forbidden_ip(ip: str) -> bool:
    """True for any address that must never be fetched server-side.

    Unparseable input is forbidden too: something that is not an address cannot
    be proven safe, and failing closed is the only correct direction here.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv4Address):
        return _is_forbidden_v4(addr)
    return _is_forbidden_v6(addr)


async def _default_lookup(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def assert_public_http_url(raw: str, lookup: LookupFn = _default_lookup) -> str:
    """Check a URL is safe to fetch, or raise :class:`ValueError` saying why.

    http and https only, no embedded credentials, and every address the host
    resolves to must be public.
    """
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"forbidden protocol {parts.scheme or '(none)'}:")
    if parts.username or parts.password:
        raise ValueError("URLs with embedded credentials are not allowed")
    host = parts.hostname
    if not host:
        raise ValueError("not a valid URL")

    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if is_forbidden_ip(host):
            raise ValueError(f"address {host} is private or reserved")
        return raw

    try:
        addrs = await lookup(host)
    except Exception:
        raise ValueError(f"cannot resolve host {host}") from None
    if not addrs:
        raise ValueError(f"host {host} resolves to no addresses")
    for addr in addrs:
        if is_forbidden_ip(addr):
            raise ValueError(f"host {host} resolves to private or reserved address {addr}")
    return raw


class _GuardedResolver(aiohttp_abc.AbstractResolver):
    """Re-check the address at connect time, closing the DNS rebind window.

    aiohttp asks this resolver for the host, so an answer that was public
    during validation and private a moment later is refused here rather than
    quietly connecting to something internal.
    """

    def __init__(self, lookup: LookupFn) -> None:
        self._lookup = lookup

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> list[dict[str, object]]:
        addrs = await self._lookup(host)
        bad = next((a for a in addrs if is_forbidden_ip(a)), None)
        if bad or not addrs:
            raise OSError(f"DNS rebind blocked: {host} resolved to {bad or 'nothing'}")
        results: list[dict[str, object]] = []
        for addr in addrs:
            fam = socket.AF_INET6 if ":" in addr else socket.AF_INET
            # Honour the family aiohttp asked for (AF_UNSPEC means either):
            # handing back the wrong one breaks connection setup on dual-stack
            # hosts.
            if family not in (socket.AF_UNSPEC, fam):
                continue
            results.append(
                {
                    "hostname": host,
                    "host": addr,
                    "port": port,
                    "family": fam,
                    "proto": 0,
                    "flags": socket.AI_NUMERICHOST,
                }
            )
        if not results:
            raise OSError(f"{host} has no addresses for the requested address family")
        return results

    async def close(self) -> None:  # pragma: no cover - nothing to release
        return None


async def fetch_public_image(
    raw_url: str,
    max_bytes: int,
    timeout_ms: float = 10_000,
    lookup: LookupFn = _default_lookup,
    redirects_left: int = 1,
) -> tuple[bytes, str]:
    """Fetch an image from an untrusted URL. Returns ``(bytes, mime)``.

    Bounded in every direction that matters: total time, declared length,
    streamed length, and one redirect hop. Raises :class:`ValueError` for
    anything that fails, so a caller can hand the reason straight back to the
    agent that supplied the URL.
    """
    url = await assert_public_http_url(raw_url, lookup)
    timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
    connector = aiohttp.TCPConnector(resolver=_GuardedResolver(lookup))
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        async with session.get(url, allow_redirects=False, headers={"accept": "image/*"}) as res:
            if res.status in _REDIRECT_STATUSES:
                location = res.headers.get("location")
                if not location:
                    raise ValueError(f"fetch {raw_url} returned HTTP {res.status} with no Location")
                if redirects_left <= 0:
                    raise ValueError(f"fetch {raw_url} followed too many redirects")
                # Resolved against the CURRENT url, then put through the whole
                # guard again. A target is not trustworthy for having been named
                # by a host that passed.
                return await fetch_public_image(
                    urljoin(url, location), max_bytes, timeout_ms, lookup, redirects_left - 1
                )
            if res.status != 200:
                raise ValueError(f"fetch {raw_url} returned HTTP {res.status}")
            declared = res.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise ValueError(f"response too large ({declared} bytes, max {max_bytes})")
            mime = (res.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
            chunks: list[bytes] = []
            total = 0
            # The declared length is a claim, not a promise. Counting what
            # actually arrives is what bounds a lying or chunked response.
            async for chunk in res.content.iter_chunked(64 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"response exceeded {max_bytes} bytes; aborting read")
                chunks.append(chunk)
            return b"".join(chunks), mime
