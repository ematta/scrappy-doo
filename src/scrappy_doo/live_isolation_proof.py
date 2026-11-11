"""Orchestrate isolation probes without issuing a security attestation.

The environment is an adapter for a future, independently controlled browser,
proxy, and receiver. This module does not provide that adapter. Observations
supplied to it can be synthetic, so its report cannot enable strict mode.
"""

from dataclasses import dataclass
from secrets import token_urlsafe
from typing import Mapping, Protocol


ALLOWED_PROBES = ("allowed_https", "allowed_private_target")
BLOCKED_PROBES = (
    "direct_navigation", "redirect", "iframe", "image", "script", "fetch",
    "websocket", "http2_cross_origin", "service_worker", "direct_tcp",
    "direct_udp", "direct_dns", "dns_over_https", "quic", "host_gateway",
    "other_container", "proxy_management", "proxy_stopped",
    "policy_unavailable", "revoked_connection", "dns_rebinding",
)


class ProofEnvironment(Protocol):
    def preflight(self) -> bool: ...
    def start(self) -> None: ...
    def configuration(self) -> object: ...
    def confirm_viewer(self) -> bool: ...
    def probe(self, case: str, token: str) -> Mapping[str, object] | None: ...
    def stop(self) -> None: ...


@dataclass(frozen=True)
class LiveProofReport:
    outcome: str
    issues: tuple[str, ...]

    @property
    def isolation_verified(self) -> bool:
        return False

    @property
    def strict_mode_available(self) -> bool:
        return False


def run_live_isolation_proof(*, environment: ProofEnvironment) -> LiveProofReport:
    """Run candidate probes; supplied observations never certify isolation.

    This is an orchestration seam, not a live environment or an attestation.
    A boundary violation takes precedence over incomplete evidence. Cleanup is
    attempted even when startup fails partway through.
    """
    issues: list[str] = []
    failed = False
    started = False
    try:
        if environment.preflight() is not True:
            issues.append("Proof preflight failed")
        else:
            started = True
            environment.start()
            configuration = environment.configuration()
            if configuration is None:
                issues.append("Initial configuration unavailable")
            if environment.confirm_viewer() is not True:
                issues.append("Viewer confirmation unavailable")
            for case in ALLOWED_PROBES + BLOCKED_PROBES:
                token = token_urlsafe(32)
                observation = environment.probe(case, token)
                if not isinstance(observation, Mapping):
                    issues.append(f"Missing observation: {case}")
                    continue
                if (observation.get("token") != token
                        or observation.get("completed") is not True
                        or observation.get("receiver_control_reachable") is not True
                        or type(observation.get("receiver_received")) is not bool
                        or type(observation.get("upstream_connected")) is not bool):
                    issues.append(f"Incomplete or uncorrelated observation: {case}")
                    continue
                received = observation["receiver_received"]
                connected = observation["upstream_connected"]
                if case in ALLOWED_PROBES:
                    if received is not True or connected is not True:
                        issues.append(f"Allowed probe did not reach its receiver: {case}")
                elif received is True or connected is True:
                    failed = True
                    issues.append(f"Denied probe reached its destination or upstream: {case}")
            if configuration != environment.configuration():
                issues.append("Runtime configuration changed during proof")
    except KeyboardInterrupt:
        issues.append("Proof interrupted")
    except Exception as exc:
        issues.append(f"Proof could not complete: {exc}")
    finally:
        if started:
            try:
                environment.stop()
            except Exception as exc:
                issues.append(f"Proof cleanup failed: {exc}")
    if not issues:
        issues.append("Observations have not been independently verified")
    return LiveProofReport(outcome="failed" if failed else "inconclusive", issues=tuple(issues))
