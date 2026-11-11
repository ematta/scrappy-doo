"""Pure destination-policy decisions for a future strict isolation proof.

These calculations do not enforce network access. Strict browser mode remains
unavailable until an actual isolation mechanism is independently verified.
"""

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class DestinationRule:
    scheme: str
    host: str
    port: int
    private_cidrs: tuple[str, ...] = ()


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str


def _canonical_host(host: str) -> str:
    if (not isinstance(host, str) or not host or any(c in host for c in "*%/@\\ \t\r\n")):
        raise ValueError("Invalid destination host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        normalized = host.rstrip(".").encode("idna").decode("ascii").lower()
        labels = normalized.split(".")
        if (not normalized or len(normalized) > 253 or any(
            not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
            or any(not (char.isascii() and (char.isalnum() or char == "-")) for char in label)
            for label in labels
        )):
            raise ValueError("Invalid destination host") from None
        return normalized
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return str(address)


def _address(value: str):
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


class StrictPolicy:
    """Evaluate a hypothetical request against explicitly allowed origins."""

    def __init__(self, *, rules: tuple[DestinationRule, ...]):
        normalized = {}
        for rule in rules:
            if (rule.scheme not in ("http", "https") or not isinstance(rule.port, int)
                    or isinstance(rule.port, bool) or not 1 <= rule.port <= 65535):
                raise ValueError("Invalid destination rule")
            key = (rule.scheme, _canonical_host(rule.host), rule.port)
            networks = tuple(ipaddress.ip_network(value, strict=True) for value in rule.private_cidrs)
            if any(network.prefixlen == 0 for network in networks):
                raise ValueError("Private destination exceptions must be scoped")
            if key in normalized:
                raise ValueError("Duplicate destination rule")
            normalized[key] = networks
        self._rules = normalized

    def check(
        self,
        *,
        url: str,
        resolved_addresses: tuple[str, ...],
        connected_address: str,
        tunnel_authority: str | None = None,
        tls_server_name: str | None = None,
        upstream_tls_verified: bool = True,
    ) -> PolicyDecision:
        denied = PolicyDecision(False, "Destination or connection is not authorized")
        try:
            if not isinstance(url, str) or any(char in url for char in "\\\r\n\t"):
                return denied
            parsed = urlsplit(url)
            if (parsed.scheme not in ("http", "https") or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None):
                return denied
            host = _canonical_host(parsed.hostname)
            port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
            if not 1 <= port <= 65535:
                return denied
            key = (parsed.scheme, host, port)
            if key not in self._rules:
                return denied
            if parsed.scheme == "https" and upstream_tls_verified is not True:
                return denied
            if tls_server_name is not None and _canonical_host(tls_server_name) != host:
                return denied
            if tunnel_authority is not None:
                authority = urlsplit("//" + tunnel_authority)
                if (not authority.hostname or authority.username is not None or authority.password is not None
                        or authority.path or authority.query or authority.fragment):
                    return denied
                tunnel_port = authority.port if authority.port is not None else 443
                if (_canonical_host(authority.hostname), tunnel_port) != (host, port):
                    return denied
            if not resolved_addresses:
                return denied
            resolved = tuple(_address(value) for value in resolved_addresses)
            connected = _address(connected_address)
            if connected not in resolved:
                return denied
            if (connected.is_loopback or connected.is_link_local or connected.is_multicast
                    or connected.is_unspecified or connected.is_reserved):
                return denied
            if not connected.is_global:
                if not any(connected.version == network.version and connected in network for network in self._rules[key]):
                    return denied
            return PolicyDecision(True, "Destination and connection match the configured rule")
        except (TypeError, ValueError, UnicodeError):
            return denied
