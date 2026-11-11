"""Unit contract for request enforcement; no live proxy or network is used."""

import unittest

from scrappy_doo.strict_policy import DestinationRule


class ResolverDouble:
    def __init__(self):
        self.answers = {"app.example": ("8.8.8.8",), "staging.internal": ("10.4.2.10",)}
        self.calls = []

    def resolve(self, host):
        self.calls.append(host)
        return self.answers.get(host, ())


class ConnectionDouble:
    def __init__(self, *, certificate_valid=True):
        self.calls = []
        self.closed = []
        self.certificate_valid = certificate_valid

    def connect(self, *, address, port, server_name, verify_tls):
        self.calls.append((address, port, server_name, verify_tls))
        if not self.certificate_valid:
            raise OSError("invalid upstream certificate")
        return len(self.calls)

    def close(self, connection):
        self.closed.append(connection)


def make_proxy(resolver, connector, *, rules=None):
    from scrappy_doo.enforcing_proxy import EnforcingProxy

    if rules is None:
        rules = (DestinationRule(scheme="https", host="app.example", port=443),)
    return EnforcingProxy(rules=rules, resolver=resolver, connector=connector)


class EnforcingProxyTests(unittest.TestCase):
    def setUp(self):
        self.resolver = ResolverDouble()
        self.connector = ConnectionDouble()

    def request(self, proxy, url="https://app.example/cart", **overrides):
        arguments = {"url": url, "tunnel_authority": "app.example:443", "tls_server_name": "app.example"}
        arguments.update(overrides)
        return proxy.request(**arguments)

    def test_connect_handshake_does_not_open_an_upstream_connection(self):
        proxy = make_proxy(self.resolver, self.connector)
        proxy.open_tunnel("app.example:443")
        self.assertEqual(self.connector.calls, [])

    def test_allowed_https_request_uses_checked_ip_and_verifies_original_tls_identity(self):
        proxy = make_proxy(self.resolver, self.connector)
        result = self.request(proxy)
        self.assertTrue(result.allowed)
        self.assertEqual(self.connector.calls, [("8.8.8.8", 443, "app.example", True)])

    def test_unapproved_host_is_denied_before_dns_or_connection(self):
        proxy = make_proxy(self.resolver, self.connector)
        result = self.request(proxy, url="https://other.example/", tunnel_authority="other.example:443", tls_server_name="other.example")
        self.assertFalse(result.allowed)
        self.assertEqual(self.resolver.calls, [])
        self.assertEqual(self.connector.calls, [])

    def test_each_request_inside_a_tunnel_is_checked_for_authority_and_tls_identity(self):
        proxy = make_proxy(self.resolver, self.connector, rules=(
            DestinationRule(scheme="https", host="app.example", port=443),
            DestinationRule(scheme="https", host="other.example", port=443),
        ))
        result = self.request(proxy, url="https://other.example/private")
        self.assertFalse(result.allowed)
        self.assertEqual(self.connector.calls, [])

    def test_private_or_metadata_dns_answer_cannot_open_an_upstream_connection(self):
        for address in ("127.0.0.1", "169.254.169.254", "10.4.2.10", "::1", "fe80::1"):
            with self.subTest(address=address):
                self.resolver.answers["app.example"] = (address,)
                proxy = make_proxy(self.resolver, self.connector)
                self.assertFalse(self.request(proxy).allowed)
                self.assertEqual(self.connector.calls, [])

    def test_private_exception_is_scoped_to_host_port_and_cidr(self):
        proxy = make_proxy(self.resolver, self.connector, rules=(
            DestinationRule(scheme="https", host="staging.internal", port=8443, private_cidrs=("10.4.0.0/16",)),
        ))
        result = self.request(proxy, url="https://staging.internal:8443/", tunnel_authority="staging.internal:8443", tls_server_name="staging.internal")
        self.assertTrue(result.allowed)
        self.assertEqual(self.connector.calls, [("10.4.2.10", 8443, "staging.internal", True)])
        self.resolver.answers["staging.internal"] = ("10.5.2.10",)
        result = self.request(proxy, url="https://staging.internal:8443/", tunnel_authority="staging.internal:8443", tls_server_name="staging.internal")
        self.assertFalse(result.allowed)
        self.assertEqual(len(self.connector.calls), 1)

    def test_dns_is_rechecked_for_each_new_connection_to_prevent_rebinding(self):
        proxy = make_proxy(self.resolver, self.connector)
        self.assertTrue(self.request(proxy).allowed)
        self.resolver.answers["app.example"] = ("127.0.0.1",)
        self.assertFalse(self.request(proxy).allowed)
        self.assertEqual(len(self.connector.calls), 1)
        self.assertEqual(self.resolver.calls, ["app.example", "app.example"])

    def test_empty_dns_answer_never_opens_an_upstream_connection(self):
        self.resolver.answers["app.example"] = ()
        proxy = make_proxy(self.resolver, self.connector)
        self.assertFalse(self.request(proxy).allowed)
        self.assertEqual(self.connector.calls, [])

    def test_malformed_request_or_mismatched_tunnel_never_opens_a_connection(self):
        proxy = make_proxy(self.resolver, self.connector)
        for url, authority, server_name in (
            ("https://user@app.example/", "app.example:443", "app.example"),
            ("https://app.example:invalid/", "app.example:443", "app.example"),
            ("https://app.example/", "other.example:443", "app.example"),
            ("https://app.example/", "app.example:443", "other.example"),
        ):
            with self.subTest(url=url, authority=authority, server_name=server_name):
                result = self.request(proxy, url=url, tunnel_authority=authority, tls_server_name=server_name)
                self.assertFalse(result.allowed)
                self.assertEqual(self.connector.calls, [])

    def test_bad_upstream_certificate_fails_closed(self):
        self.connector.certificate_valid = False
        proxy = make_proxy(self.resolver, self.connector)
        self.assertFalse(self.request(proxy).allowed)
        self.assertTrue(self.connector.calls[0][3])

    def test_policy_revocation_closes_existing_connections_and_denies_new_requests(self):
        proxy = make_proxy(self.resolver, self.connector)
        self.assertTrue(self.request(proxy).allowed)
        proxy.update_policy(rules=())
        self.assertEqual(self.connector.closed, [1])
        self.assertFalse(self.request(proxy).allowed)
        self.assertEqual(len(self.connector.calls), 1)

    def test_invalid_policy_update_revokes_existing_access(self):
        proxy = make_proxy(self.resolver, self.connector)
        self.assertTrue(self.request(proxy).allowed)
        with self.assertRaises((ValueError, TypeError)):
            proxy.update_policy(rules=(DestinationRule(scheme="https", host="*", port=443),))
        self.assertEqual(self.connector.closed, [1])
        self.assertFalse(self.request(proxy).allowed)

    def test_missing_or_invalid_policy_does_not_open_a_connection(self):
        for rules in (None, (DestinationRule(scheme="https", host="*.example", port=443),)):
            with self.subTest(rules=rules):
                from scrappy_doo.enforcing_proxy import EnforcingProxy
                with self.assertRaises((ValueError, TypeError)):
                    EnforcingProxy(rules=rules, resolver=self.resolver, connector=self.connector)
                self.assertEqual(self.connector.calls, [])

    def test_denial_does_not_disclose_secret_url_content(self):
        proxy = make_proxy(self.resolver, self.connector)
        result = self.request(proxy, url="https://other.example/?token=private-value", tunnel_authority="other.example:443", tls_server_name="other.example")
        self.assertFalse(result.allowed)
        self.assertNotIn("private-value", str(result))


if __name__ == "__main__":
    unittest.main()
