"""Contract for live-proof orchestration; doubles do not prove isolation.

These tests never launch containers, open sockets, or certify a real boundary.
The independent receiver and isolated-browser operations are represented by
separate doubles so that a browser error alone cannot count as denial.
"""

import unittest

from scrappy_doo.browser import BrowserConfig, BrowserError, BrowserSession


class FakeProofEnvironment:
    """A controllable stand-in for the proposed live-proof environment."""

    def __init__(self):
        self.events = []
        self.preflight_ok = True
        self.setup_error = None
        self.probe_error = None
        self.cleanup_error = None
        self.configuration_changed = False
        self.viewer_confirmed = True
        self.bad_case = None
        self.unreachable_control = None
        self.missing_observation = None
        self.upstream_case = None
        self.stale_token = None
        self.cancelled = False

    def preflight(self):
        self.events.append("preflight")
        return self.preflight_ok

    def start(self):
        self.events.append("start")
        if self.setup_error:
            raise self.setup_error

    def configuration(self):
        self.events.append("configuration")
        if self.configuration_changed and self.events.count("configuration") > 1:
            return "changed-configuration"
        return "initial-configuration"

    def confirm_viewer(self):
        self.events.append("viewer")
        return self.viewer_confirmed

    def probe(self, case, token):
        self.events.append(("probe", case, token))
        if self.cancelled:
            raise KeyboardInterrupt()
        if self.probe_error:
            raise self.probe_error
        if case == self.missing_observation:
            return None
        allowed = case in {"allowed_https", "allowed_private_target"}
        return {
            "token": "stale-token" if case == self.stale_token else token,
            "completed": True,
            "receiver_control_reachable": case != self.unreachable_control,
            "receiver_received": not allowed if case == self.bad_case else allowed,
            "upstream_connected": (allowed or case == self.upstream_case),
        }

    def stop(self):
        self.events.append("stop")
        if self.cleanup_error:
            raise self.cleanup_error


def run(environment):
    # Import here so discovery still runs the existing suite when the new
    # production entry point has not been implemented yet.
    from scrappy_doo.live_isolation_proof import run_live_isolation_proof

    return run_live_isolation_proof(environment=environment)


class LiveIsolationProofTests(unittest.TestCase):
    def setUp(self):
        self.environment = FakeProofEnvironment()

    def test_preflight_failure_does_not_start_the_environment(self):
        self.environment.preflight_ok = False
        report = run(self.environment)
        self.assertEqual(report.outcome, "inconclusive")
        self.assertIn("preflight", " ".join(report.issues).lower())
        self.assertNotIn("start", self.environment.events)
        self.assertFalse(report.strict_mode_available)

    def test_runner_exercises_both_allowed_and_blocked_probe_categories(self):
        report = run(self.environment)
        cases = {event[1] for event in self.environment.events if isinstance(event, tuple)}
        self.assertTrue({
            "allowed_https", "allowed_private_target", "redirect", "fetch",
            "websocket", "http2_cross_origin", "service_worker", "direct_tcp",
            "direct_udp", "direct_dns", "dns_over_https", "quic", "host_gateway",
            "other_container", "proxy_management", "proxy_stopped",
            "policy_unavailable", "revoked_connection", "dns_rebinding",
        }.issubset(cases))
        self.assertIn("stop", self.environment.events)
        self.assertFalse(report.strict_mode_available)

    def test_doubled_environment_cannot_certify_real_isolation(self):
        report = run(self.environment)
        self.assertFalse(report.isolation_verified)
        self.assertFalse(report.strict_mode_available)
        self.assertNotEqual(report.outcome, "verified")

    def test_failed_allowed_request_is_not_a_successful_proof(self):
        self.environment.bad_case = "allowed_https"
        report = run(self.environment)
        self.assertNotEqual(report.outcome, "verified")
        self.assertIn("allowed_https", " ".join(report.issues))

    def test_denied_destination_receiving_a_request_fails_the_proof(self):
        self.environment.bad_case = "redirect"
        report = run(self.environment)
        self.assertEqual(report.outcome, "failed")
        self.assertIn("redirect", " ".join(report.issues))
        self.assertFalse(report.strict_mode_available)

    def test_denied_upstream_connection_fails_even_without_received_request(self):
        self.environment.upstream_case = "http2_cross_origin"
        report = run(self.environment)
        self.assertEqual(report.outcome, "failed")
        self.assertIn("http2_cross_origin", " ".join(report.issues))

    def test_unreachable_receiver_control_makes_denial_inconclusive(self):
        self.environment.unreachable_control = "direct_tcp"
        report = run(self.environment)
        self.assertEqual(report.outcome, "inconclusive")
        self.assertIn("direct_tcp", " ".join(report.issues))

    def test_missing_probe_observation_is_not_counted_as_a_denial(self):
        self.environment.missing_observation = "quic"
        report = run(self.environment)
        self.assertEqual(report.outcome, "inconclusive")
        self.assertIn("quic", " ".join(report.issues))

    def test_each_probe_uses_a_distinct_token_and_rejects_stale_observations(self):
        self.environment.stale_token = "fetch"
        report = run(self.environment)
        tokens = [event[2] for event in self.environment.events if isinstance(event, tuple)]
        self.assertEqual(len(tokens), len(set(tokens)))
        self.assertEqual(report.outcome, "inconclusive")
        self.assertIn("fetch", " ".join(report.issues))

    def test_configuration_change_invalidates_the_run(self):
        self.environment.configuration_changed = True
        report = run(self.environment)
        self.assertEqual(report.outcome, "inconclusive")
        self.assertIn("configuration", " ".join(report.issues).lower())
        self.assertFalse(report.isolation_verified)

    def test_viewer_must_be_confirmed_for_manual_sign_in(self):
        self.environment.viewer_confirmed = False
        report = run(self.environment)
        self.assertEqual(report.outcome, "inconclusive")
        self.assertIn("viewer", " ".join(report.issues).lower())
        self.assertFalse(report.strict_mode_available)

    def test_partial_start_is_cleaned_up_and_cannot_verify_isolation(self):
        self.environment.setup_error = RuntimeError("startup failed")
        report = run(self.environment)
        self.assertIn("stop", self.environment.events)
        self.assertEqual(report.outcome, "inconclusive")
        self.assertIn("startup failed", " ".join(report.issues))

    def test_probe_error_and_interruption_both_clean_up(self):
        for interrupt in (False, True):
            with self.subTest(interrupt=interrupt):
                environment = FakeProofEnvironment()
                environment.cancelled = interrupt
                if not interrupt:
                    environment.probe_error = RuntimeError("probe failed")
                report = run(environment)
                self.assertIn("stop", environment.events)
                self.assertNotEqual(report.outcome, "verified")
                self.assertFalse(report.strict_mode_available)

    def test_cleanup_failure_is_visible_and_prevents_verification(self):
        self.environment.cleanup_error = RuntimeError("cleanup failed")
        report = run(self.environment)
        self.assertNotEqual(report.outcome, "verified")
        self.assertIn("cleanup failed", " ".join(report.issues))
        self.assertFalse(report.strict_mode_available)

    def test_fake_proof_never_unlocks_the_existing_strict_browser(self):
        report = run(self.environment)
        self.assertFalse(report.isolation_verified)

        class Transport:
            def start(self, command):
                raise AssertionError("strict mode must not start")

        browser = BrowserSession(
            config=BrowserConfig(allowed_origins=("https://app.example",), boundary_mode="strict"),
            transport=Transport(),
        )
        with self.assertRaises(BrowserError):
            browser.start()


if __name__ == "__main__":
    unittest.main()
