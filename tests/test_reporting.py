"""Proposed report contract for coverage review."""

import unittest

from scrappy_doo.execution import CleanupStatus, Execution, ExecutionStatus, Finding, FindingResult
from scrappy_doo.reporting import format_report


class ReportTests(unittest.TestCase):
    def test_report_shows_expected_observed_evidence_and_cleanup_separately(self):
        finding = Finding(
            expected="Total is $20", observed="Total is $25", result=FindingResult.FAILED,
            evidence=("cart.txt",),
        )
        execution = Execution(findings=(finding,), goal_verified=False, cleanup_status=CleanupStatus.FAILED)

        report = format_report(execution)

        for value in ("Total is $20", "Total is $25", "cart.txt", "failed", "cleanup"):
            self.assertIn(value.lower(), report.lower())

    def test_inconclusive_report_includes_reason(self):
        finding = Finding(
            expected="Cart loads", observed="Page unavailable", result=FindingResult.INCONCLUSIVE,
            evidence=(), reason="Browser disconnected",
        )

        report = format_report(Execution(findings=(finding,), goal_verified=False))

        self.assertIn("inconclusive", report.lower())
        self.assertIn("Browser disconnected", report)

    def test_interrupted_report_keeps_partial_finding_without_final_pass(self):
        finding = Finding(expected="Cart loads", observed="Cart loaded", result=FindingResult.PASSED, evidence=("cart.txt",))
        execution = Execution(findings=(finding,), goal_verified=False, status=ExecutionStatus.INTERRUPTED)

        report = format_report(execution)

        self.assertIn("interrupted", report.lower())
        self.assertIn("Cart loaded", report)
        self.assertNotIn("overall: passed", report.lower())

    def test_report_distinguishes_agent_result_from_reviewed_result_and_reason(self):
        finding = Finding(expected="Total is $20", observed="Total is $25", result=FindingResult.FAILED, evidence=("cart.txt",))
        finding.review(FindingResult.PASSED, reason="Discount applied separately")

        report = format_report(Execution(findings=(finding,), goal_verified=True))

        self.assertIn("failed", report.lower())
        self.assertIn("passed", report.lower())
        self.assertIn("Discount applied separately", report)


if __name__ == "__main__":
    unittest.main()
