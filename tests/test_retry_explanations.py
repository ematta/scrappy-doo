"""Proposed regressions for explanations that claim an unrecorded retry."""

import unittest

from scrappy_doo.execution import Finding, FindingResult
from scrappy_doo.orchestration import (
    ExecutionRunner,
    FinalAnswer,
    FindingUpdate,
    RunPolicy,
    Tool,
    ToolCall,
)
from scrappy_doo.reporting import format_report


class ScriptedModel:
    def __init__(self, *responses):
        self.responses = iter(responses)

    def respond(self, *, goal, observations):
        return next(self.responses)


def blocked_finding(reason):
    return Finding(
        expected="Cart total is $20",
        observed="Cart service unavailable (HTTP 503)",
        result=FindingResult.INCONCLUSIVE,
        evidence=("observations/cart.txt",),
        reason=reason,
    )


class RetryExplanationTests(unittest.TestCase):
    def run_agent(self, actions, reason, *, extra_responses=()):
        calls = []

        def inspect(*, target):
            calls.append(target)
            return "HTTP 503 Service Unavailable"

        responses = [ToolCall(name="inspect", arguments={"target": target}) for target in actions]
        responses.extend(extra_responses)
        responses.append(FinalAnswer(findings=(blocked_finding(reason),), goal_verified=False))
        runner = ExecutionRunner(
            model=ScriptedModel(*responses),
            tools={"inspect": Tool(handler=inspect, required_arguments=("target",))},
        )
        execution = runner.run(
            goal="Verify the cart total",
            policy=RunPolicy(allowed_tools=frozenset({"inspect"}), max_tool_calls=10),
        )
        return execution, calls

    def assert_retry_is_disclosed_as_unverified(self, finding):
        explanation = (finding.reason or "").lower()
        self.assertTrue(
            any(text in explanation for text in ("unverified", "not verified", "not recorded", "no retry")),
            explanation,
        )

    def test_final_report_does_not_claim_an_unperformed_retry(self):
        reason = "The cart page returned 503 Service Unavailable. Retrying once before finalizing."

        execution, calls = self.run_agent(["cart"], reason)

        self.assertEqual(calls, ["cart"])
        self.assertEqual(execution.overall_result, FindingResult.INCONCLUSIVE)
        self.assertEqual(execution.findings[0].observed, "Cart service unavailable (HTTP 503)")
        self.assertEqual(execution.findings[0].evidence, ("observations/cart.txt",))
        self.assert_retry_is_disclosed_as_unverified(execution.findings[0])
        self.assertNotIn("retrying once before finalizing", format_report(execution).lower())

    def test_past_tense_retry_claim_is_not_accepted_after_one_attempt(self):
        execution, calls = self.run_agent(["cart"], "Retried the cart once; it remained unavailable.")

        self.assertEqual(calls, ["cart"])
        self.assert_retry_is_disclosed_as_unverified(execution.findings[0])
        self.assertNotIn("retried the cart once", format_report(execution).lower())

    def test_an_actual_retry_of_the_same_action_can_be_reported(self):
        reason = "The cart returned 503. Retried the cart once; it remained unavailable."

        execution, calls = self.run_agent(["cart", "cart"], reason)

        self.assertEqual(calls, ["cart", "cart"])
        self.assertEqual(execution.findings[0].reason, reason)

    def test_a_different_action_does_not_count_as_a_retry(self):
        execution, calls = self.run_agent(
            ["cart", "checkout"], "The cart returned 503. Retried the cart once; it remained unavailable.",
        )

        self.assertEqual(calls, ["cart", "checkout"])
        self.assert_retry_is_disclosed_as_unverified(execution.findings[0])

    def test_retrying_a_different_target_does_not_support_a_cart_retry_claim(self):
        execution, calls = self.run_agent(
            ["cart", "checkout", "checkout"],
            "The cart returned 503. Retried the cart once; it remained unavailable.",
        )

        self.assertEqual(calls, ["cart", "checkout", "checkout"])
        self.assert_retry_is_disclosed_as_unverified(execution.findings[0])

    def test_claiming_two_retries_requires_more_than_one_retry(self):
        execution, calls = self.run_agent(
            ["cart", "cart"], "The cart returned 503. Retried the cart twice; it remained unavailable.",
        )

        self.assertEqual(calls, ["cart", "cart"])
        self.assert_retry_is_disclosed_as_unverified(execution.findings[0])

    def test_truthful_statement_that_no_retry_occurred_is_preserved(self):
        reason = "The cart returned 503. No retry was performed."

        execution, _ = self.run_agent(["cart"], reason)

        self.assertEqual(execution.findings[0].reason, reason)

    def test_recommending_a_future_retry_does_not_claim_one_happened(self):
        reason = "The cart returned 503. A retry may help investigate the outage."

        execution, _ = self.run_agent(["cart"], reason)

        self.assertEqual(execution.findings[0].reason, reason)

    def test_unverified_claim_in_a_partial_finding_is_not_reported_as_fact(self):
        model = ScriptedModel(
            ToolCall(name="inspect", arguments={"target": "cart"}),
            FindingUpdate(finding=blocked_finding("The cart returned 503. Retried the cart once.")),
            FinalAnswer(findings=(), goal_verified=False),
        )
        runner = ExecutionRunner(
            model=model,
            tools={"inspect": Tool(handler=lambda target: "HTTP 503 Service Unavailable", required_arguments=("target",))},
        )

        execution = runner.run(
            goal="Verify cart", policy=RunPolicy(allowed_tools=frozenset({"inspect"}), max_tool_calls=5),
        )

        self.assert_retry_is_disclosed_as_unverified(execution.findings[0])
        self.assertNotIn("retried the cart once", format_report(execution).lower())


if __name__ == "__main__":
    unittest.main()
