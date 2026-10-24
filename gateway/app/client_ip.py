"""Client address used by the per-process rate limit.

``TRUSTED_PROXIES`` is a comma-separated list of CIDRs and IPs. It is empty
by default, and an empty list means the direct peer is the client: both
``X-Forwarded-For`` and ``X-Real-IP`` are ignored, including when a client
sends them.

When the direct peer falls inside that list, the client address is taken from
the headers that peer is trusted to set:

- ``X-Forwarded-For`` is a hop list. Walking from the right, trusted hops are
  skipped and the rightmost hop that is not a trusted proxy is the client.
  Addresses further left were supplied by that hop and are ignored, so a
  client cannot choose its own bucket by prepending a spoofed address.
- If ``X-Forwarded-For`` is absent, ``X-Real-IP`` is that single client
  address.
- A header that is not a single valid IP cannot mint a new bucket. The key
  falls back to the direct peer.

Only the direct connection is authenticated. Headers are consulted solely
because that peer is one of ours.
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache


def parse_trusted_proxies(value: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse a comma-separated CIDR list. Blank means no trusted proxies."""
    if not value or not value.strip():
        return ()
    parts = tuple(part.strip() for part in value.split(","))
    if any(part == "" for part in parts):
        raise ValueError("TRUSTED_PROXIES contains an empty entry")
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for part in parts:
        try:
            networks.append(ipaddress.ip_network(part, strict=True))
        except ValueError as exc:
            raise ValueError(f"TRUSTED_PROXIES entry {part!r} is not a valid CIDR or IP") from exc
    return tuple(networks)


@lru_cache(maxsize=32)
def cached_trusted_proxies(value: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return parse_trusted_proxies(value)


def client_ip(scope, trusted_proxies: str) -> str:
    """Return the rate-limit key for one ASGI HTTP scope."""
    networks = cached_trusted_proxies(trusted_proxies)
    direct_host = _direct_host(scope)
    direct_ip = _parse_ip(direct_host) if direct_host else None
    if direct_ip is None or not networks or not _is_trusted(direct_ip, networks):
        return _key(direct_ip, direct_host)

    forwarded = _header_values(scope, b"x-forwarded-for")
    if forwarded:
        hops = [hop.strip() for hop in ",".join(forwarded).split(",") if hop.strip()]
        for hop in reversed(hops):
            hop_ip = _parse_ip(hop)
            if hop_ip is None:
                # Not an IP, so it cannot be a trusted proxy and it must not
                # become its own bucket. Everything to its left is untrusted too.
                return _key(direct_ip, direct_host)
            if not _is_trusted(hop_ip, networks):
                return _key(hop_ip, direct_host)
        if hops:
            leftmost = _parse_ip(hops[0])
            if leftmost is not None:
                return _key(leftmost, direct_host)
        return _key(direct_ip, direct_host)

    real_ip_values = _header_values(scope, b"x-real-ip")
    if len(real_ip_values) == 1:
        real_ip = _parse_ip(real_ip_values[0])
        if real_ip is not None:
            return _key(real_ip, direct_host)
    return _key(direct_ip, direct_host)


def _direct_host(scope) -> str | None:
    client = scope.get("client")
    if not client:
        return None
    host = client[0]
    if not isinstance(host, str) or not host:
        return None
    return host


def _header_values(scope, name: bytes) -> list[str]:
    values: list[str] = []
    for key, value in scope.get("headers") or ():
        if key.lower() != name:
            continue
        text = value.decode("latin-1").strip()
        if text:
            values.append(text)
    return values


def _parse_ip(value: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if value is None:
        return None
    text = value.strip().strip('"').strip("'")
    if not text:
        return None
    if text.startswith("["):
        end = text.find("]")
        if end <= 1:
            return None
        text = text[1:end]
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def _equivalents(ip: ipaddress.IPv4Address | ipaddress.IPv6Address):
    yield ip
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        yield ip.ipv4_mapped


def _is_trusted(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, networks) -> bool:
    return any(candidate in network for candidate in _equivalents(ip) for network in networks)


def _key(ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None, fallback: str | None) -> str:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped.compressed
    if ip is not None:
        return ip.compressed
    return fallback or "unknown"
