"""HTTPS proxy protocol contract with independent test doubles, no sockets."""

import unittest

from scrappy_doo.strict_policy import DestinationRule


class Resolver:
    def __init__(self):
        self.address = "8.8.8.8"
        self.calls = []

    def resolve(self, host):
        self.calls.append(host)
        return (self.address,)


class Upstream:
    def __init__(self):
        self.calls = []
        self.closed = []
        self.invalid_certificate = False

    def exchange(self, *, address, port, server_name, verify_tls, method, path, headers, body):
        self.calls.append((address, port, server_name, verify_tls, method, path, headers, body))
        if self.invalid_certificate:
            raise OSError("upstream TLS verification failed")
        return {"status": 200, "headers": {"content-type": "text/plain"}, "body": b"ok"}

    def close_all(self):
        self.closed.append(True)


class CertificateAuthority:
    def __init__(self):
        self.hosts = []
        self.fail = False

    def certificate_for(self, hostname):
        self.hosts.append(hostname)
        if self.fail:
            raise OSError("CA unavailable")
        return "leaf-for-" + hostname


def make_server(resolver, upstream, authority):
    from scrappy_doo.https_intercepting_proxy import HTTPSInterceptingProxy
    return HTTPSInterceptingProxy(
        rules=(DestinationRule(scheme="https", host="app.example", port=443),),
        resolver=resolver, upstream=upstream, certificate_authority=authority,
    )


class HTTPSInterceptingProxyTests(unittest.TestCase):
    def setUp(self):
        self.resolver = Resolver()
        self.upstream = Upstream()
        self.authority = CertificateAuthority()

    def connect(self, server, authority="app.example:443"):
        return server.connect(authority=authority, client_server_name="app.example")

    def request(self, server, **overrides):
        args = {
            "tunnel_authority": "app.example:443", "client_server_name": "app.example",
            "method": "GET", "url": "https://app.example/cart", "headers": {}, "body": b"",
        }
        args.update(overrides)
        return server.request(**args)

    def test_connect_mints_matching_leaf_without_opening_upstream(self):
        response = self.connect(make_server(self.resolver, self.upstream, self.authority))
        self.assertTrue(response.allowed)
        self.assertEqual(response.certificate, "leaf-for-app.example")
        self.assertEqual(self.upstream.calls, [])

    def test_disallowed_connect_does_not_create_a_certificate_or_open_upstream(self):
        server = make_server(self.resolver, self.upstream, self.authority)
        response = self.connect(server, authority="other.example:443")
        self.assertFalse(response.allowed)
        self.assertEqual(self.authority.hosts, [])
        self.assertEqual(self.upstream.calls, [])

    def test_ca_failure_does_not_create_a_tunnel(self):
        self.authority.fail = True
        server = make_server(self.resolver, self.upstream, self.authority)
        self.assertFalse(self.connect(server).allowed)
        self.assertEqual(self.upstream.calls, [])

    def test_connect_with_mismatched_tls_server_name_is_rejected(self):
        server = make_server(self.resolver, self.upstream, self.authority)
        response = server.connect(authority="app.example:443", client_server_name="other.example")
        self.assertFalse(response.allowed)
        self.assertEqual(self.authority.hosts, [])
        self.assertEqual(self.upstream.calls, [])

    def test_allowed_decrypted_request_is_forwarded_using_checked_ip_and_tls(self):
        server = make_server(self.resolver, self.upstream, self.authority)
        response = self.request(server, method="POST", headers={"content-type": "text/plain"}, body=b"hello")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b"ok")
        self.assertEqual(self.upstream.calls, [("8.8.8.8", 443, "app.example", True, "POST", "/cart", {"content-type": "text/plain"}, b"hello")])

    def test_disallowed_inner_authority_is_rejected_before_dns_or_upstream(self):
        server = make_server(self.resolver, self.upstream, self.authority)
        response = self.request(server, url="https://other.example/secret")
        self.assertEqual(response.status, 403)
        self.assertEqual(self.resolver.calls, [])
        self.assertEqual(self.upstream.calls, [])

    def test_host_header_cannot_disagree_with_the_authorized_url(self):
        server = make_server(self.resolver, self.upstream, self.authority)
        response = self.request(server, headers={"Host": "other.example"})
        self.assertEqual(response.status, 403)
        self.assertEqual(self.upstream.calls, [])

    def test_unsupported_protocol_fails_closed(self):
        server = make_server(self.resolver, self.upstream, self.authority)
        response = self.request(server, protocol="http/3")
        self.assertNotEqual(response.status, 200)
        self.assertEqual(self.upstream.calls, [])

    def test_rebinding_and_invalid_upstream_certificate_fail_closed(self):
        server = make_server(self.resolver, self.upstream, self.authority)
        self.resolver.address = "127.0.0.1"
        self.assertEqual(self.request(server).status, 403)
        self.assertEqual(self.upstream.calls, [])
        self.resolver.address = "8.8.8.8"
        self.upstream.invalid_certificate = True
        self.assertNotEqual(self.request(server).status, 200)
        self.assertTrue(self.upstream.calls[0][3])

    def test_policy_revocation_closes_connections_and_denies_subsequent_requests(self):
        server = make_server(self.resolver, self.upstream, self.authority)
        self.assertEqual(self.request(server).status, 200)
        server.update_policy(rules=())
        self.assertTrue(self.upstream.closed)
        self.assertEqual(self.request(server).status, 403)
        self.assertEqual(len(self.upstream.calls), 1)

    def test_diagnostics_do_not_expose_headers_body_or_query(self):
        server = make_server(self.resolver, self.upstream, self.authority)
        response = self.request(server, url="https://other.example/?token=secret-query", headers={"Authorization": "secret-header"}, body=b"secret-body")
        for secret in ("secret-query", "secret-header", "secret-body"):
            self.assertNotIn(secret, str(response))


if __name__ == "__main__":
    unittest.main()
