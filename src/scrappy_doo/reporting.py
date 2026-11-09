"""Human-readable execution reports."""

from scrappy_doo.execution import Execution


def format_report(execution: Execution) -> str:
    overall = execution.overall_result.value if execution.overall_result is not None else "not final"
    lines = [
        f"Status: {execution.status.value}",
        f"Overall: {overall}",
        f"Cleanup: {execution.cleanup_status.value}",
    ]
    for index, finding in enumerate(execution.findings):
        lines.extend([
            "",
            f"Finding {index}: {finding.current_result.value}",
            f"Expected: {finding.expected}",
            f"Observed: {finding.observed}",
            f"Evidence: {', '.join(finding.evidence) if finding.evidence else 'none'}",
        ])
        if finding.reason:
            lines.append(f"Reason: {finding.reason}")
        if finding.reviews:
            lines.append(f"Agent result: {finding.original_result.value}")
            for review in finding.reviews:
                lines.append(f"Review: {review.result.value} — {review.reason}")
    return "\n".join(lines)
