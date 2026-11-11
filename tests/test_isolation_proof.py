"""Proposed strict-mode proof checks using simulated observations only.

Passing this test file must never be treated as proof of real network isolation.
"""

import unittest
from dataclasses import replace

from scrappy_doo.browser import BrowserConfig, BrowserError, BrowserSession
from scrappy_doo.isolation_proof import ProofConfiguration, ProbeObservation, assess_simulation


# The acceptance cases are stated here independently of the implementation.
ALLOWED = ("allowed_https", "allowed_private_target")
BLOCKED = (
    "direct_navigation", "redirect", "iframe", "image", "script", "fetch", "websocket",
    "http2_cross_origin", "service_worker", "direct_tcp", "direct_udp", "direct_dns",
    "dns_over_https", "quic", "host_gateway", "other_container", "proxy_management",
    "proxy_stopped", "policy_unavailable", "revoked_connection", "dns_rebinding",
)


def configuration():
    return ProofConfiguration(
        macos_version="fixture-macos",
        docker_version="fixture-docker",
        browser_image_digest="sha256:" + "a" * 64,
        proxy_image_digest="sha256:" + "b" * 64,
        browser_version="fixture-browser",
        mcp_version="fixture-mcp",
        policy_digest="sha256:" + "c" * 64,
        per_request_tls_inspection=True,
        interception_ca_browser_only=True,
        upstream_tls_verified=True,
        traffic_content_logging=False,
        viewer_authenticated=True,
        viewer_loopback_only=True,
        viewer_clipboard_enabled=False,
        viewer_file_transfer_enabled=False,
        browser_privileged=False,
        browser_has_docker_socket=False,
        browser_extra_networks=False,
        proxy_management_exposed=False,
    )


def observations():
    return tuple(
        ProbeObservation(
            name=name, completed=True, control_reachable=True,
            destination_received=True, upstream_connection_attempted=True,
        ) for name in ALLOWED
    ) + tuple(
        ProbeObservation(
            name=name, completed=True, control_reachable=True,
            destination_received=False, upstream_connection_attempted=False,
        ) for name in BLOCKED
    )


def change_observation(items, name, **changes):
    return tuple(replace(item, **changes) if item.name == name else item for item in items)


class SimulationAssessmentTests(unittest.TestCase):
    def test_complete_simulation_can_pass_checks_but_cannot_enable_strict_mode(self):
        report = assess_simulation(configuration=configuration(), observations=observations())

        self.assertTrue(report.checks_passed)
        self.assertFalse(report.isolation_verified)
        self.assertFalse(report.strict_mode_available)

    def test_simulated_success_cannot_unlock_the_current_browser_session(self):
        report = assess_simulation(configuration=configuration(), observations=observations())
        self.assertTrue(report.checks_passed)

        class TransportDouble:
            def __init__(self):
                self.commands = []

            def start(self, command):
                self.commands.append(command)

        transport = TransportDouble()
        browser = BrowserSession(
            config=BrowserConfig(allowed_origins=("https://app.example",), boundary_mode="strict"),
            transport=transport,
        )
        with self.assertRaises(BrowserError):
            browser.start()
        self.assertEqual(transport.commands, [])

    def test_every_required_probe_must_have_an_observation(self):
        for missing in (*ALLOWED, *BLOCKED):
            with self.subTest(missing=missing):
                incomplete = tuple(item for item in observations() if item.name != missing)
                report = assess_simulation(configuration=configuration(), observations=incomplete)
                self.assertFalse(report.checks_passed)
                self.assertIn(missing, " ".join(report.issues))

    def test_duplicate_probe_result_is_not_treated_as_extra_evidence(self):
        items = observations() + (observations()[0],)

        report = assess_simulation(configuration=configuration(), observations=items)

        self.assertFalse(report.checks_passed)
        self.assertIn("duplicate", " ".join(report.issues).lower())

    def test_allowed_destination_must_actually_receive_the_request(self):
        items = change_observation(observations(), "allowed_https", destination_received=False)

        report = assess_simulation(configuration=configuration(), observations=items)

        self.assertFalse(report.checks_passed)
        self.assertIn("allowed_https", " ".join(report.issues))

    def test_blocked_destination_receiving_a_request_fails_the_assessment(self):
        items = change_observation(observations(), "redirect", destination_received=True)

        report = assess_simulation(configuration=configuration(), observations=items)

        self.assertFalse(report.checks_passed)
        self.assertIn("redirect", " ".join(report.issues))

    def test_forbidden_upstream_connection_fails_even_without_an_http_request(self):
        items = change_observation(observations(), "http2_cross_origin", upstream_connection_attempted=True)

        report = assess_simulation(configuration=configuration(), observations=items)

        self.assertFalse(report.checks_passed)
        self.assertIn("http2_cross_origin", " ".join(report.issues))

    def test_absence_of_a_request_is_not_proof_when_the_receiver_control_is_unreachable(self):
        items = change_observation(observations(), "direct_tcp", control_reachable=False)

        report = assess_simulation(configuration=configuration(), observations=items)

        self.assertFalse(report.checks_passed)
        self.assertIn("direct_tcp", " ".join(report.issues))

    def test_incomplete_probe_is_not_counted_as_blocked(self):
        items = change_observation(observations(), "quic", completed=False)

        report = assess_simulation(configuration=configuration(), observations=items)

        self.assertFalse(report.checks_passed)
        self.assertIn("quic", " ".join(report.issues))

    def test_stale_or_changed_runtime_configuration_cannot_reuse_results(self):
        original = configuration()
        changed = replace(original, policy_digest="sha256:" + "d" * 64)

        report = assess_simulation(configuration=original, observations=observations(), current_configuration=changed)

        self.assertFalse(report.checks_passed)
        self.assertIn("configuration", " ".join(report.issues).lower())

    def test_browser_and_proxy_versions_and_images_must_be_pinned(self):
        for field, value in (("browser_image_digest", "latest"), ("proxy_image_digest", ""),
                             ("browser_version", ""), ("mcp_version", ""), ("docker_version", "")):
            with self.subTest(field=field):
                report = assess_simulation(configuration=replace(configuration(), **{field: value}), observations=observations())
                self.assertFalse(report.checks_passed)

    def test_missing_per_request_inspection_or_upstream_verification_fails(self):
        for field in ("per_request_tls_inspection", "interception_ca_browser_only", "upstream_tls_verified"):
            with self.subTest(field=field):
                report = assess_simulation(configuration=replace(configuration(), **{field: False}), observations=observations())
                self.assertFalse(report.checks_passed)

    def test_viewer_must_be_authenticated_local_and_without_clipboard_or_file_transfer(self):
        for field, value in (("viewer_authenticated", False), ("viewer_loopback_only", False),
                             ("viewer_clipboard_enabled", True), ("viewer_file_transfer_enabled", True)):
            with self.subTest(field=field):
                report = assess_simulation(configuration=replace(configuration(), **{field: value}), observations=observations())
                self.assertFalse(report.checks_passed)

    def test_browser_and_proxy_management_must_not_have_unrestricted_access(self):
        for field in ("browser_privileged", "browser_has_docker_socket", "browser_extra_networks", "proxy_management_exposed"):
            with self.subTest(field=field):
                report = assess_simulation(configuration=replace(configuration(), **{field: True}), observations=observations())
                self.assertFalse(report.checks_passed)

    def test_proxy_content_logging_is_disabled_for_the_initial_proof(self):
        report = assess_simulation(configuration=replace(configuration(), traffic_content_logging=True), observations=observations())

        self.assertFalse(report.checks_passed)

    def test_empty_observations_cannot_claim_a_proof(self):
        report = assess_simulation(configuration=configuration(), observations=())

        self.assertFalse(report.checks_passed)
        self.assertFalse(report.isolation_verified)
        self.assertFalse(report.strict_mode_available)


if __name__ == "__main__":
    unittest.main()
