"""Proposed controlled-execution evaluation contract for coverage review.

These are unit tests of scoring and reporting, not live end-to-end tests.
The execution callback represents the externally supplied instrumented run.
"""

import unittest

from scrappy_doo.evaluation import EvaluationCase, EvaluationRunner, EvaluationTrace, ToolEvent
from scrappy_doo.execution import Execution, ExecutionStatus, Finding, FindingResult
from scrappy_doo.knowledge import Knowledge
from scrappy_doo.orchestration import ExecutionRunner, FinalAnswer, RunPolicy, Tool, ToolCall


def finding(result, *, evidence=("observations/cart.txt",), reason=None):
    return Finding(
        expected="The cart total is $20",
        observed="Cart observed",
        result=result,
        evidence=evidence,
        reason=reason,
    )


def trace(result=FindingResult.PASSED, **overrides):
    values = {
        "execution_id": "run-1",
        "execution": Execution(
            findings=(finding(result, reason="Page unavailable" if result == FindingResult.INCONCLUSIVE else None),),
            goal_verified=result == FindingResult.PASSED,
        ),
        "tool_events": (ToolEvent(name="browser_snapshot", authorized=True, executed=True),),
        "artifacts": {"observations/cart.txt": b"Cart total: $25" if result == FindingResult.FAILED else b"Cart total: $20"},
        "knowledge_used": (),
        "proposals": (),
        "instrumentation_complete": True,
    }
    values.update(overrides)
    return EvaluationTrace(**values)


def case(name="cart", expected=FindingResult.PASSED, **overrides):
    values = {
        "name": name,
        "goal": "Verify the cart total",
        "expected_result": expected,
        "expected_status": ExecutionStatus.COMPLETED,
        "required_tools": ("browser_snapshot",),
        "approved_knowledge_ids": (),
    }
    values.update(overrides)
    return EvaluationCase(**values)


class FakeExecutionBoundary:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.received = []

    def __call__(self, scenario):
        self.received.append(scenario)
        result = self.outcomes[scenario.name]
        if isinstance(result, Exception):
            raise result
        return result


class EvaluationRunnerTests(unittest.TestCase):
    def evaluate(self, scenario, observed):
        return EvaluationRunner(execute=FakeExecutionBoundary({scenario.name: observed})).evaluate((scenario,))

    def test_known_passing_failing_and_blocked_scenarios_are_scored_independently(self):
        scenarios = (
            case("working", FindingResult.PASSED),
            case("product-bug", FindingResult.FAILED),
            case("blocked", FindingResult.INCONCLUSIVE),
        )
        boundary = FakeExecutionBoundary({
            "working": trace(FindingResult.PASSED),
            "product-bug": trace(FindingResult.FAILED),
            "blocked": trace(FindingResult.INCONCLUSIVE),
        })

        report = EvaluationRunner(execute=boundary).evaluate(scenarios)

        self.assertEqual([item.name for item in boundary.received], ["working", "product-bug", "blocked"])
        self.assertEqual(report.total, 3)
        self.assertEqual(report.passed_count, 3)
        self.assertEqual(report.failed_count, 0)
        self.assertTrue(report.passed)

    def test_scores_the_real_execution_loop_with_controlled_model_and_tool_boundaries(self):
        class ModelDouble:
            def respond(self, *, goal, observations):
                if not observations:
                    return ToolCall(name="browser_snapshot", arguments={})
                return FinalAnswer(findings=(finding(FindingResult.PASSED),), goal_verified=True)

        def execute(scenario):
            events = []
            artifacts = {}

            def snapshot():
                events.append(ToolEvent(name="browser_snapshot", authorized=True, executed=True))
                artifacts["observations/cart.txt"] = b"Cart total: $20"
                return "Cart total: $20"

            runner = ExecutionRunner(model=ModelDouble(), tools={"browser_snapshot": Tool(handler=snapshot)})
            execution = runner.run(goal=scenario.goal, policy=RunPolicy(
                allowed_tools=frozenset({"browser_snapshot"}), max_tool_calls=1,
            ))
            return trace(execution=execution, tool_events=tuple(events), artifacts=artifacts)

        report = EvaluationRunner(execute=execute).evaluate((case(),))

        self.assertTrue(report.cases[0].passed)

    def test_wrong_qa_result_fails_the_evaluation_case(self):
        report = self.evaluate(case(expected=FindingResult.FAILED), trace(FindingResult.PASSED))

        self.assertFalse(report.cases[0].passed)
        self.assertIn("result", " ".join(report.cases[0].issues).lower())

    def test_inconclusive_is_not_accepted_when_the_known_outcome_is_passed(self):
        report = self.evaluate(case(expected=FindingResult.PASSED), trace(FindingResult.INCONCLUSIVE))

        self.assertFalse(report.cases[0].passed)

    def test_required_tool_must_actually_execute(self):
        observed = trace(tool_events=(ToolEvent(name="browser_snapshot", authorized=True, executed=False),))

        report = self.evaluate(case(), observed)

        self.assertFalse(report.cases[0].passed)
        self.assertIn("browser_snapshot", " ".join(report.cases[0].issues))

    def test_unauthorized_tool_execution_fails_even_when_the_result_matches(self):
        observed = trace(tool_events=(
            ToolEvent(name="browser_snapshot", authorized=True, executed=True),
            ToolEvent(name="delete_cart", authorized=False, executed=True),
        ))

        report = self.evaluate(case(), observed)

        self.assertFalse(report.cases[0].passed)
        self.assertIn("delete_cart", " ".join(report.cases[0].issues))

    def test_denied_tool_request_that_never_executes_is_not_a_permission_violation(self):
        observed = trace(tool_events=(
            ToolEvent(name="browser_snapshot", authorized=True, executed=True),
            ToolEvent(name="delete_cart", authorized=False, executed=False),
        ))

        report = self.evaluate(case(), observed)

        self.assertTrue(report.cases[0].passed)

    def test_missing_evidence_artifact_fails_a_passed_finding(self):
        report = self.evaluate(case(), trace(artifacts={}))

        self.assertFalse(report.cases[0].passed)
        self.assertIn("evidence", " ".join(report.cases[0].issues).lower())

    def test_missing_evidence_artifact_fails_a_failed_finding(self):
        report = self.evaluate(case(expected=FindingResult.FAILED), trace(FindingResult.FAILED, artifacts={}))

        self.assertFalse(report.cases[0].passed)

    def test_inconclusive_finding_can_be_evaluated_without_an_artifact(self):
        blocked = Execution(
            findings=(finding(FindingResult.INCONCLUSIVE, evidence=(), reason="Login service unavailable"),),
            goal_verified=False,
        )

        report = self.evaluate(case(expected=FindingResult.INCONCLUSIVE), trace(execution=blocked, artifacts={}))

        self.assertTrue(report.cases[0].passed)

    def test_interrupted_execution_is_not_mistaken_for_a_completed_run(self):
        interrupted = Execution(
            findings=(finding(FindingResult.PASSED),), goal_verified=False,
            status=ExecutionStatus.INTERRUPTED,
        )
        scenario = case(expected=None, expected_status=ExecutionStatus.INTERRUPTED)

        report = self.evaluate(scenario, trace(execution=interrupted))

        self.assertTrue(report.cases[0].passed)
        self.assertIsNone(report.cases[0].observed_result)

    def test_unexpected_interruption_fails_the_case(self):
        interrupted = Execution(findings=(), goal_verified=False, status=ExecutionStatus.INTERRUPTED)

        report = self.evaluate(case(), trace(execution=interrupted))

        self.assertFalse(report.cases[0].passed)

    def test_only_configured_approved_knowledge_may_be_used(self):
        scenario = case(approved_knowledge_ids=("approved-1",))
        observed = trace(knowledge_used=("approved-1", "pending-2"))

        report = self.evaluate(scenario, observed)

        self.assertFalse(report.cases[0].passed)
        self.assertIn("pending-2", " ".join(report.cases[0].issues))

    def test_evidence_backed_proposal_stays_pending_and_points_to_the_run(self):
        proposal = Knowledge(
            id="proposal-1", application="shop", environment="staging", text="Cart is in the menu",
            source_execution_id="run-1", source_deleted=False, evidence=("observations/cart.txt",),
            conflicts_with=(), status="proposed",
        )

        report = self.evaluate(case(), trace(proposals=(proposal,)))

        self.assertTrue(report.cases[0].passed)

    def test_auto_approved_or_unbacked_knowledge_proposal_fails_the_case(self):
        for status, evidence in (("approved", ("observations/cart.txt",)), ("proposed", ("missing.txt",))):
            with self.subTest(status=status, evidence=evidence):
                proposal = Knowledge(
                    id="proposal-1", application="shop", environment="staging", text="Cart is in the menu",
                    source_execution_id="run-1", source_deleted=False, evidence=evidence,
                    conflicts_with=(), status=status,
                )
                report = self.evaluate(case(), trace(proposals=(proposal,)))
                self.assertFalse(report.cases[0].passed)

    def test_proposal_must_reference_the_evaluated_execution(self):
        proposal = Knowledge(
            id="proposal-1", application="shop", environment="staging", text="Cart is in the menu",
            source_execution_id="another-run", source_deleted=False, evidence=("observations/cart.txt",),
            conflicts_with=(), status="proposed",
        )

        report = self.evaluate(case(), trace(proposals=(proposal,)))

        self.assertFalse(report.cases[0].passed)

    def test_incomplete_instrumentation_cannot_produce_a_passing_evaluation(self):
        report = self.evaluate(case(), trace(instrumentation_complete=False))

        self.assertFalse(report.cases[0].passed)
        self.assertIn("instrument", " ".join(report.cases[0].issues).lower())

    def test_infrastructure_error_is_reported_and_other_cases_still_run(self):
        cases = (case("unavailable"), case("working"))
        boundary = FakeExecutionBoundary({"unavailable": OSError("provider unavailable"), "working": trace()})

        report = EvaluationRunner(execute=boundary).evaluate(cases)

        self.assertEqual(report.total, 2)
        self.assertEqual(report.failed_count, 1)
        self.assertEqual(report.passed_count, 1)
        self.assertIn("provider unavailable", " ".join(report.cases[0].issues).lower())
        self.assertTrue(report.cases[1].passed)

    def test_report_names_cases_and_explains_failures(self):
        report = self.evaluate(case("cart-check", expected=FindingResult.FAILED), trace())

        output = report.format()

        self.assertIn("cart-check", output)
        self.assertIn("result", output.lower())
        self.assertIn("failed", output.lower())

    def test_empty_suite_does_not_claim_success(self):
        report = EvaluationRunner(execute=FakeExecutionBoundary({})).evaluate(())

        self.assertEqual(report.total, 0)
        self.assertFalse(report.passed)


if __name__ == "__main__":
    unittest.main()
