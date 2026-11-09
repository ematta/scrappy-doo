"""Behavioral contract for findings and execution results.

The public API used here is provisional and is intended for coverage review
before the execution model is implemented.
"""

import unittest

from scrappy_doo.execution import (
    CleanupStatus,
    Execution,
    ExecutionStatus,
    Finding,
    FindingResult,
)


def finding(result: FindingResult) -> Finding:
    return Finding(
        expected="The cart total is $20",
        observed="The cart total is $20" if result == FindingResult.PASSED else "The cart total is $25",
        result=result,
        evidence=("observations/cart-total.txt",),
        reason="The expected outcome could not be established" if result == FindingResult.INCONCLUSIVE else None,
    )


class FindingTests(unittest.TestCase):
    def test_finding_exposes_expected_and_observed_outcomes_and_evidence(self):
        result = finding(FindingResult.FAILED)

        self.assertEqual(result.expected, "The cart total is $20")
        self.assertEqual(result.observed, "The cart total is $25")
        self.assertEqual(result.current_result, FindingResult.FAILED)
        self.assertEqual(result.evidence, ("observations/cart-total.txt",))

    def test_inconclusive_finding_explains_what_could_not_be_verified(self):
        result = Finding(
            expected="The cart total is $20",
            observed="The cart page did not load",
            result=FindingResult.INCONCLUSIVE,
            evidence=("observations/page-error.txt",),
            reason="The expected total could not be observed",
        )

        self.assertEqual(result.current_result, FindingResult.INCONCLUSIVE)
        self.assertEqual(result.reason, "The expected total could not be observed")


class OverallResultTests(unittest.TestCase):
    def test_fully_verified_goal_with_only_passing_findings_passes(self):
        execution = Execution(findings=(finding(FindingResult.PASSED),), goal_verified=True)

        self.assertEqual(execution.overall_result, FindingResult.PASSED)

    def test_failure_takes_precedence_over_inconclusive_and_passing_findings(self):
        execution = Execution(
            findings=(finding(FindingResult.PASSED), finding(FindingResult.INCONCLUSIVE), finding(FindingResult.FAILED)),
            goal_verified=False,
        )

        self.assertEqual(execution.overall_result, FindingResult.FAILED)

    def test_inconclusive_finding_prevents_an_otherwise_passing_result(self):
        execution = Execution(
            findings=(finding(FindingResult.PASSED), finding(FindingResult.INCONCLUSIVE)),
            goal_verified=False,
        )

        self.assertEqual(execution.overall_result, FindingResult.INCONCLUSIVE)

    def test_passing_findings_do_not_pass_an_unverified_goal(self):
        execution = Execution(findings=(finding(FindingResult.PASSED),), goal_verified=False)

        self.assertEqual(execution.overall_result, FindingResult.INCONCLUSIVE)

    def test_no_findings_does_not_pass_a_goal(self):
        execution = Execution(findings=(), goal_verified=False)

        self.assertEqual(execution.overall_result, FindingResult.INCONCLUSIVE)


class InterruptionAndCleanupTests(unittest.TestCase):
    def test_interrupted_execution_retains_partial_findings_and_evidence(self):
        partial = finding(FindingResult.PASSED)
        execution = Execution(
            findings=(partial,), goal_verified=False, status=ExecutionStatus.INTERRUPTED
        )

        self.assertEqual(execution.status, ExecutionStatus.INTERRUPTED)
        self.assertEqual(execution.findings[0].evidence, ("observations/cart-total.txt",))
        self.assertEqual(execution.findings[0].current_result, FindingResult.PASSED)

    def test_interruption_status_is_preserved_when_a_partial_finding_failed(self):
        execution = Execution(
            findings=(finding(FindingResult.FAILED),),
            goal_verified=False,
            status=ExecutionStatus.INTERRUPTED,
        )

        self.assertEqual(execution.status, ExecutionStatus.INTERRUPTED)
        self.assertEqual(execution.findings[0].current_result, FindingResult.FAILED)

    def test_failed_cleanup_is_reported_separately_from_a_passing_goal(self):
        execution = Execution(
            findings=(finding(FindingResult.PASSED),),
            goal_verified=True,
            cleanup_status=CleanupStatus.FAILED,
        )

        self.assertEqual(execution.overall_result, FindingResult.PASSED)
        self.assertEqual(execution.cleanup_status, CleanupStatus.FAILED)


class HumanReviewTests(unittest.TestCase):
    def test_review_changes_current_result_but_preserves_agent_assessment(self):
        result = finding(FindingResult.FAILED)

        result.review(FindingResult.PASSED, reason="The discount was applied separately")

        self.assertEqual(result.original_result, FindingResult.FAILED)
        self.assertEqual(result.current_result, FindingResult.PASSED)
        self.assertEqual(result.reviews[0].reason, "The discount was applied separately")

    def test_reviewed_result_determines_the_overall_result(self):
        result = finding(FindingResult.FAILED)
        result.review(FindingResult.PASSED, reason="The expected total was incorrect")
        execution = Execution(findings=(result,), goal_verified=True)

        self.assertEqual(execution.overall_result, FindingResult.PASSED)

    def test_review_revision_retains_prior_decisions_and_reasons(self):
        result = finding(FindingResult.FAILED)
        result.review(FindingResult.PASSED, reason="The discount was applied separately")
        result.review(FindingResult.INCONCLUSIVE, reason="The discount evidence was incomplete")

        self.assertEqual(result.original_result, FindingResult.FAILED)
        self.assertEqual(result.current_result, FindingResult.INCONCLUSIVE)
        self.assertEqual(
            [(review.result, review.reason) for review in result.reviews],
            [
                (FindingResult.PASSED, "The discount was applied separately"),
                (FindingResult.INCONCLUSIVE, "The discount evidence was incomplete"),
            ],
        )

    def test_review_requires_a_reason(self):
        result = finding(FindingResult.FAILED)

        with self.assertRaises(ValueError):
            result.review(FindingResult.PASSED, reason="")


if __name__ == "__main__":
    unittest.main()
