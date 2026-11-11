"""Evaluate simulated strict-isolation proof inputs without granting access.

This module does not run Docker, inspect a real network, or issue an isolation
attestation. All returned assessments leave strict mode unavailable.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ProofConfiguration:
    macos_version: str
    docker_version: str
    browser_image_digest: str
    proxy_image_digest: str
    browser_version: str
    mcp_version: str
    policy_digest: str
    per_request_tls_inspection: bool
    interception_ca_browser_only: bool
    upstream_tls_verified: bool
    traffic_content_logging: bool
    viewer_authenticated: bool
    viewer_loopback_only: bool
    viewer_clipboard_enabled: bool
    viewer_file_transfer_enabled: bool
    browser_privileged: bool
    browser_has_docker_socket: bool
    browser_extra_networks: bool
    proxy_management_exposed: bool


@dataclass(frozen=True)
class ProbeObservation:
    name: str
    completed: bool
    control_reachable: bool
    destination_received: bool
    upstream_connection_attempted: bool


@dataclass(frozen=True)
class RuntimeObservation:
    docker_available: bool
    browser_internal_network_only: bool
    direct_egress_blocked: bool
    direct_dns_blocked: bool
    proxy_alive: bool
    proxy_management_isolated: bool
    policy_loaded: bool
    policy_digest: str
    tls_inspection_active: bool
    upstream_tls_verified: bool
    viewer_guarded: bool


@dataclass(frozen=True)
class SimulationAssessment:
    checks_passed: bool
    issues: tuple[str, ...]

    @property
    def isolation_verified(self) -> bool:
        return False

    @property
    def strict_mode_available(self) -> bool:
        return False


@dataclass(frozen=True)
class PreconditionAssessment:
    eligible_for_probe: bool
    issues: tuple[str, ...]

    @property
    def isolation_verified(self) -> bool:
        return False

    @property
    def strict_mode_available(self) -> bool:
        return False


_ALLOWED = frozenset({"allowed_https", "allowed_private_target"})
_BLOCKED = frozenset({
    "direct_navigation", "redirect", "iframe", "image", "script", "fetch", "websocket",
    "http2_cross_origin", "service_worker", "direct_tcp", "direct_udp", "direct_dns",
    "dns_over_https", "quic", "host_gateway", "other_container", "proxy_management",
    "proxy_stopped", "policy_unavailable", "revoked_connection", "dns_rebinding",
})
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def assess_simulation(
    *,
    configuration: ProofConfiguration,
    observations: tuple[ProbeObservation, ...],
    current_configuration: ProofConfiguration | None = None,
) -> SimulationAssessment:
    """Score synthetic inputs only; a passing simulation is not a proof."""
    issues = []
    if current_configuration is not None and current_configuration != configuration:
        issues.append("Runtime configuration differs from the assessed configuration")
    for field in ("macos_version", "docker_version", "browser_version", "mcp_version"):
        value = getattr(configuration, field)
        if not isinstance(value, str) or not value.strip() or value.strip().lower() == "latest":
            issues.append(f"Configuration requires a pinned {field}")
    for field in ("browser_image_digest", "proxy_image_digest", "policy_digest"):
        value = getattr(configuration, field)
        if not isinstance(value, str) or not _DIGEST.fullmatch(value):
            issues.append(f"Configuration requires a pinned {field}")
    required_true = (
        "per_request_tls_inspection", "interception_ca_browser_only", "upstream_tls_verified",
        "viewer_authenticated", "viewer_loopback_only",
    )
    required_false = (
        "traffic_content_logging", "viewer_clipboard_enabled", "viewer_file_transfer_enabled",
        "browser_privileged", "browser_has_docker_socket", "browser_extra_networks", "proxy_management_exposed",
    )
    for field in required_true:
        if getattr(configuration, field) is not True:
            issues.append(f"Configuration requires {field}")
    for field in required_false:
        if getattr(configuration, field) is not False:
            issues.append(f"Configuration forbids {field}")

    by_name = {}
    for observation in observations:
        if observation.name in by_name:
            issues.append(f"Duplicate probe observation: {observation.name}")
        elif observation.name not in _ALLOWED | _BLOCKED:
            issues.append(f"Unknown probe observation: {observation.name}")
        else:
            by_name[observation.name] = observation
    for name in sorted(_ALLOWED | _BLOCKED):
        observation = by_name.get(name)
        if observation is None:
            issues.append(f"Missing probe observation: {name}")
            continue
        if observation.completed is not True or observation.control_reachable is not True:
            issues.append(f"Incomplete probe or unreachable control: {name}")
            continue
        expected = name in _ALLOWED
        if observation.destination_received is not expected or observation.upstream_connection_attempted is not expected:
            issues.append(f"Unexpected destination or upstream connection: {name}")
    return SimulationAssessment(checks_passed=not issues, issues=tuple(issues))


def assess_runtime_preconditions(*, expected_policy_digest: str, observed: RuntimeObservation) -> PreconditionAssessment:
    """Assess supplied observations as probe prerequisites, not isolation proof."""
    issues = []
    for field in (
        "docker_available", "browser_internal_network_only", "direct_egress_blocked", "direct_dns_blocked",
        "proxy_alive", "proxy_management_isolated", "policy_loaded", "tls_inspection_active",
        "upstream_tls_verified", "viewer_guarded",
    ):
        if getattr(observed, field) is not True:
            issues.append(f"Runtime precondition not met: {field}")
    if (not isinstance(expected_policy_digest, str) or not _DIGEST.fullmatch(expected_policy_digest)
            or observed.policy_digest != expected_policy_digest):
        issues.append("Runtime policy digest does not match")
    return PreconditionAssessment(eligible_for_probe=not issues, issues=tuple(issues))
