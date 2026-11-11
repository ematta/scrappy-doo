"""Authorization logic for a future TLS-intercepting proxy transport.

This module is not a listener and does not terminate TLS. The certificate
issuer and upstream exchange are injected; a production network transport and
independent isolation verification are required before it can enforce a boundary.
"""

from dataclasses import dataclass, field
from threading import RLock
from urllib.parse import urlsplit

from scrappy_doo.strict_policy import StrictPolicy


@dataclass(frozen=True)
class TunnelResult:
    allowed: bool
    certificate: object = field(default=None, repr=False)


@dataclass(frozen=True)
class ProxyResponse:
    status: int
    headers: dict = field(default_factory=dict, repr=False)
    body: bytes = field(default=b"", repr=False)


class HTTPSInterceptingProxy:
    def __init__(self, *, rules, resolver, upstream, certificate_authority):
        self._policy = StrictPolicy(rules=rules)
        self._resolver = resolver
        self._upstream = upstream
        self._ca = certificate_authority
        self._lock = RLock()

    def _check(self, *, url, tunnel_authority, client_server_name, addresses, selected):
        return self._policy.check(
            url=url, resolved_addresses=addresses, connected_address=selected,
            tunnel_authority=tunnel_authority, tls_server_name=client_server_name,
        )

    def connect(self, *, authority, client_server_name):
        with self._lock:
            if not isinstance(authority, str) or not isinstance(client_server_name, str):
                return TunnelResult(False)
            allowed = self._check(
                url=f"https://{authority}/", tunnel_authority=authority,
                client_server_name=client_server_name, addresses=("8.8.8.8",), selected="8.8.8.8",
            )
            if not allowed.allowed:
                return TunnelResult(False)
            try:
                host = urlsplit(f"https://{authority}").hostname.rstrip(".").encode("idna").decode("ascii").lower()
                certificate = self._ca.certificate_for(host)
                if certificate is None:
                    return TunnelResult(False)
                return TunnelResult(True, certificate)
            except (OSError, ValueError, TypeError, UnicodeError):
                return TunnelResult(False)

    def request(self, *, tunnel_authority, client_server_name, method, url, headers, body, protocol="http/1.1"):
        with self._lock:
            denied = ProxyResponse(403)
            if (protocol.lower() not in {"http/1.1", "http/1.0"} if isinstance(protocol, str) else True):
                return ProxyResponse(501)
            if (not isinstance(tunnel_authority, str) or not isinstance(client_server_name, str)
                    or not isinstance(method, str) or not method.isascii() or not method.isalpha()
                    or not isinstance(headers, dict) or not isinstance(body, bytes)):
                return denied
            precheck = self._check(
                url=url, tunnel_authority=tunnel_authority, client_server_name=client_server_name,
                addresses=("8.8.8.8",), selected="8.8.8.8",
            )
            if not precheck.allowed:
                return denied
            try:
                parsed = urlsplit(url)
                if parsed.scheme != "https" or parsed.fragment:
                    return denied
                host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
                port = parsed.port or 443
                host_headers = []
                for name, value in headers.items():
                    if (not isinstance(name, str) or not isinstance(value, str)
                            or any(c in name + value for c in "\r\n")):
                        return denied
                    if name.lower() == "host":
                        host_headers.append(value)
                if len(host_headers) > 1:
                    return denied
                if host_headers and not self._check(
                    url=url, tunnel_authority=host_headers[0], client_server_name=client_server_name,
                    addresses=("8.8.8.8",), selected="8.8.8.8",
                ).allowed:
                    return denied
                addresses = tuple(self._resolver.resolve(host))
                for address in addresses:
                    if not self._check(
                        url=url, tunnel_authority=tunnel_authority, client_server_name=client_server_name,
                        addresses=addresses, selected=address,
                    ).allowed:
                        continue
                    path = parsed.path or "/"
                    if parsed.query:
                        path += "?" + parsed.query
                    response = self._upstream.exchange(
                        address=address, port=port, server_name=host, verify_tls=True,
                        method=method, path=path, headers=headers, body=body,
                    )
                    return ProxyResponse(response["status"], response["headers"], response["body"])
            except (OSError, ValueError, TypeError, UnicodeError, KeyError):
                return ProxyResponse(502)
            return denied

    def update_policy(self, *, rules):
        with self._lock:
            self._policy = StrictPolicy(rules=())
            self._upstream.close_all()
            self._policy = StrictPolicy(rules=rules)
