"""Proposed strict-proof precondition checks; no Docker or network access."""

import unittest
from dataclasses import replace

from scrappy_doo.isolation_proof import RuntimeObservation, assess_runtime_preconditions


def healthy_state():
    return RuntimeObservation(
        docker_available=True,
        browser_internal_network_only=True,
        direct_egress_blocked=True,
        direct_dns_blocked=True,
        proxy_alive=True,
        proxy_management_isolated=True,
        policy_loaded=True,
        policy_digest="sha256:" + "a" * 64,
        tls_inspection_active=True,
        upstream_tls_verified=True,
        viewer_guarded=True,
    )


class StrictProofPreconditionTests(unittest.TestCase):
    def assess(self, state):
        return assess_runtime_preconditions(expected_policy_digest="sha256:" + "a" * 64, observed=state)

    def test_healthy_simulated_state_is_only_eligible_for_probing(self):
        result = self.assess(healthy_state())

        self.assertTrue(result.eligible_for_probe)
        self.assertFalse(result.isolation_verified)
        self.assertFalse(result.strict_mode_available)

    def test_missing_docker_or_network_boundary_prevents_probing(self):
        for field in ("docker_available", "browser_internal_network_only", "direct_egress_blocked", "direct_dns_blocked"):
            with self.subTest(field=field):
                result = self.assess(replace(healthy_state(), **{field: False}))
                self.assertFalse(result.eligible_for_probe)
                self.assertFalse(result.strict_mode_available)

    def test_proxy_policy_tls_and_viewer_must_be_available(self):
        for field in ("proxy_alive", "proxy_management_isolated", "policy_loaded", "tls_inspection_active",
                      "upstream_tls_verified", "viewer_guarded"):
            with self.subTest(field=field):
                result = self.assess(replace(healthy_state(), **{field: False}))
                self.assertFalse(result.eligible_for_probe)
                self.assertFalse(result.strict_mode_available)

    def test_stale_policy_digest_prevents_probing(self):
        result = self.assess(replace(healthy_state(), policy_digest="sha256:" + "b" * 64))

        self.assertFalse(result.eligible_for_probe)
        self.assertIn("policy", " ".join(result.issues).lower())

    def test_a_later_proxy_failure_invalidates_a_previous_healthy_observation(self):
        before = self.assess(healthy_state())
        after = self.assess(replace(healthy_state(), proxy_alive=False))

        self.assertTrue(before.eligible_for_probe)
        self.assertFalse(after.eligible_for_probe)
        self.assertFalse(after.strict_mode_available)


if __name__ == "__main__":
    unittest.main()
