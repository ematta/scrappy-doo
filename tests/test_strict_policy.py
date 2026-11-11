"""Proposed pure policy tests; these do not establish network isolation."""

import unittest

from scrappy_doo.strict_policy import DestinationRule, StrictPolicy


class StrictPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = StrictPolicy(rules=(DestinationRule(scheme="https", host="app.example", port=443),))

    def check(self, url, *, resolved=("8.8.8.8",), connected="8.8.8.8", **kwargs):
        return self.policy.check(
            url=url, resolved_addresses=resolved, connected_address=connected, **kwargs,
        )

    def test_exact_allowed_https_destination_can_be_authorized(self):
        self.assertTrue(self.check("https://app.example/cart").allowed)

    def test_scheme_hostname_and_port_are_all_part_of_the_rule(self):
        for url in ("http://app.example/cart", "https://app.example:8443/cart", "https://other.example/cart"):
            with self.subTest(url=url):
                self.assertFalse(self.check(url).allowed)

    def test_hostname_comparison_does_not_allow_subdomains_or_suffix_tricks(self):
        for url in ("https://sub.app.example/", "https://app.example.evil.test/", "https://notapp.example/"):
            with self.subTest(url=url):
                self.assertFalse(self.check(url).allowed)

    def test_case_and_default_port_are_normalized(self):
        self.assertTrue(self.check("https://APP.Example:443/cart").allowed)

    def test_internationalized_hostname_uses_one_canonical_form(self):
        policy = StrictPolicy(rules=(DestinationRule(scheme="https", host="bücher.example", port=443),))

        decision = policy.check(
            url="https://xn--bcher-kva.example/", resolved_addresses=("8.8.8.8",), connected_address="8.8.8.8",
        )

        self.assertTrue(decision.allowed)

    def test_empty_policy_denies_all_destinations(self):
        policy = StrictPolicy(rules=())

        self.assertFalse(policy.check(
            url="https://app.example/", resolved_addresses=("8.8.8.8",), connected_address="8.8.8.8",
        ).allowed)

    def test_broad_wildcard_rules_are_rejected(self):
        for host in ("*", "*.example.com"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                StrictPolicy(rules=(DestinationRule(scheme="https", host=host, port=443),))

    def test_credential_bearing_or_malformed_urls_are_denied(self):
        for url in ("https://user@app.example/", "https://app.example:invalid/", "file:///etc/passwd"):
            with self.subTest(url=url):
                self.assertFalse(self.check(url).allowed)

    def test_loopback_link_local_and_private_addresses_are_denied_by_default(self):
        for address in ("127.0.0.1", "169.254.169.254", "10.4.2.10", "::1", "fe80::1", "fd00::1"):
            with self.subTest(address=address):
                self.assertFalse(self.check("https://app.example/", resolved=(address,), connected=address).allowed)

    def test_explicit_private_rule_allows_only_its_host_port_and_cidr(self):
        self.policy = StrictPolicy(rules=(DestinationRule(
            scheme="https", host="staging.internal", port=8443, private_cidrs=("10.4.0.0/16",),
        ),))

        self.assertTrue(self.check("https://staging.internal:8443/", resolved=("10.4.2.10",), connected="10.4.2.10").allowed)
        self.assertFalse(self.check("https://staging.internal:8443/", resolved=("10.5.2.10",), connected="10.5.2.10").allowed)
        self.assertFalse(self.check("https://staging.internal/", resolved=("10.4.2.10",), connected="10.4.2.10").allowed)

    def test_private_exception_does_not_authorize_metadata_or_unrelated_addresses(self):
        self.policy = StrictPolicy(rules=(DestinationRule(
            scheme="https", host="staging.internal", port=443, private_cidrs=("10.4.0.0/16",),
        ),))
        for address in ("169.254.169.254", "127.0.0.1", "10.5.0.5"):
            with self.subTest(address=address):
                self.assertFalse(self.check("https://staging.internal/", resolved=(address,), connected=address).allowed)

    def test_connected_address_must_be_one_of_the_checked_dns_answers(self):
        decision = self.check("https://app.example/", resolved=("8.8.8.8",), connected="1.1.1.1")

        self.assertFalse(decision.allowed)

    def test_each_connection_is_checked_when_dns_returns_public_and_private_addresses(self):
        resolved = ("8.8.8.8", "10.4.2.10")

        self.assertTrue(self.check("https://app.example/", resolved=resolved, connected="8.8.8.8").allowed)
        self.assertFalse(self.check("https://app.example/", resolved=resolved, connected="10.4.2.10").allowed)

    def test_empty_dns_answer_or_invalid_address_fails_closed(self):
        self.assertFalse(self.check("https://app.example/", resolved=(), connected="8.8.8.8").allowed)
        self.assertFalse(self.check("https://app.example/", resolved=("not-an-ip",), connected="not-an-ip").allowed)

    def test_public_ipv6_is_checked_without_requiring_ipv4(self):
        address = "2606:4700:4700::1111"

        self.assertTrue(self.check("https://app.example/", resolved=(address,), connected=address).allowed)

    def test_tls_identity_must_match_the_approved_destination(self):
        self.assertFalse(self.check(
            "https://app.example/", tls_server_name="other.example", upstream_tls_verified=True,
        ).allowed)

    def test_invalid_upstream_certificate_is_not_accepted(self):
        self.assertFalse(self.check("https://app.example/", upstream_tls_verified=False).allowed)

    def test_an_allowed_request_cannot_be_fronted_through_another_tunnel(self):
        self.policy = StrictPolicy(rules=(
            DestinationRule(scheme="https", host="app.example", port=443),
            DestinationRule(scheme="https", host="other.example", port=443),
        ))

        decision = self.check(
            "https://other.example/", tunnel_authority="app.example:443", tls_server_name="app.example",
        )

        self.assertFalse(decision.allowed)

    def test_denial_diagnostics_do_not_expose_url_query_values(self):
        secret = "fictional-private-token"

        decision = self.check(f"https://other.example/?token={secret}")

        self.assertFalse(decision.allowed)
        self.assertNotIn(secret, str(decision))


if __name__ == "__main__":
    unittest.main()
