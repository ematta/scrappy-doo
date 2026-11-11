"""Request authorization and connection orchestration for a future proxy.

This module is not an HTTP proxy server. The resolver and connector must be
provided by an adapter; no such production adapter is supplied. It cannot by
itself intercept browser TLS or provide a network boundary.
"""

from threading import RLock
from urllib.parse import urlsplit

from scrappy_doo.strict_policy import PolicyDecision, StrictPolicy


_DENIED = PolicyDecision(False, "Destination or connection is not authorized")


class EnforcingProxy:
    """Authorize a request before asking the connector for a checked address."""

    def __init__(self, *, rules, resolver, connector):
        self._policy = StrictPolicy(rules=rules)
        self._resolver = resolver
        self._connector = connector
        self._connections = []
        self._lock = RLock()

    def open_tunnel(self, authority):
        """Authorize the CONNECT authority without creating an upstream socket."""
        with self._lock:
            if not isinstance(authority, str):
                return _DENIED
            return self._policy.check(
                url=f"https://{authority}/", resolved_addresses=("8.8.8.8",),
                connected_address="8.8.8.8", tunnel_authority=authority,
            )

    def request(self, *, url, tunnel_authority, tls_server_name):
        with self._lock:
            if not isinstance(tunnel_authority, str) or not isinstance(tls_server_name, str):
                return _DENIED
            # This first check uses a synthetic public address only to validate
            # the origin and tunnel before DNS. It never authorizes a socket.
            precheck = self._policy.check(
                url=url, resolved_addresses=("8.8.8.8",), connected_address="8.8.8.8",
                tunnel_authority=tunnel_authority, tls_server_name=tls_server_name,
            )
            if not precheck.allowed:
                return _DENIED
            try:
                parsed = urlsplit(url)
                host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                addresses = tuple(self._resolver.resolve(host))
                for address in addresses:
                    decision = self._policy.check(
                        url=url, resolved_addresses=addresses, connected_address=address,
                        tunnel_authority=tunnel_authority, tls_server_name=tls_server_name,
                    )
                    if not decision.allowed:
                        continue
                    connection = self._connector.connect(
                        address=address, port=port, server_name=host,
                        verify_tls=parsed.scheme == "https",
                    )
                    self._connections.append(connection)
                    return decision
            except (OSError, ValueError, TypeError, UnicodeError):
                return _DENIED
            return _DENIED

    def update_policy(self, *, rules):
        """Revoke connections before installing new rules; invalid rules deny all."""
        with self._lock:
            self._policy = StrictPolicy(rules=())
            pending, self._connections = self._connections, []
            failures = []
            for connection in pending:
                try:
                    self._connector.close(connection)
                except Exception as exc:
                    failures.append(exc)
            if failures:
                raise RuntimeError("Unable to close revoked connections") from failures[0]
            self._policy = StrictPolicy(rules=rules)
