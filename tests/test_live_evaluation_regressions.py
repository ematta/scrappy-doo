"""Regression coverage proposed after the first live sample evaluation."""

import json
import unittest

from scrappy_doo.evaluation import EvaluationCase, EvaluationRunner, EvaluationTrace, ToolEvent
from scrappy_doo.execution import Execution, Finding, FindingResult
from scrappy_doo.openrouter import OpenRouterError, OpenRouterModel
from scrappy_doo.controlled_evaluation import build_scenarios


def final_response():
    return {
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps({
            "goal_verified": True,
            "findings": [{
                "expected": "Cart total is $20", "observed": "Cart total is $20",
                "result": "passed", "evidence": ["cart.txt"], "reason": None,
            }],
            "knowledge_proposals": [],
        })}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0},
    }


class CaptureTransport:
    def __init__(self, response=None):
        self.response = response or final_response()
        self.body = None

    def __call__(self, *, url, headers, body):
        self.body = body
        return self.response


class ProviderStructuredOutputRegressionTests(unittest.TestCase):
    def test_request_requires_a_structured_final_answer_and_supported_provider(self):
        transport = CaptureTransport()
        model = OpenRouterModel(api_key="fixture-key", tool_schemas={}, transport=transport)

        model.respond(goal="Verify the cart total", observations=())

        response_format = transport.body["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertTrue(response_format["json_schema"]["strict"])
        schema = response_format["json_schema"]["schema"]
        self.assertTrue({"goal_verified", "findings", "knowledge_proposals"}.issubset(schema["required"]))
        self.assertTrue(transport.body["provider"]["require_parameters"])

    def test_request_retains_tool_calling_when_structured_output_is_enabled(self):
        transport = CaptureTransport()
        model = OpenRouterModel(
            api_key="fixture-key", transport=transport,
            tool_schemas={"browser_snapshot": {"type": "object", "properties": {}, "required": []}},
        )

        model.respond(goal="Verify the cart total", observations=())

        self.assertEqual(transport.body["tools"][0]["function"]["name"], "browser_snapshot")
        self.assertEqual(transport.body["response_format"]["type"], "json_schema")

    def test_unstructured_prose_with_fenced_json_is_not_silently_accepted(self):
        response = final_response()
        raw = response["choices"][0]["message"]["content"]
        response["choices"][0]["message"]["content"] = f"The cart looks good.\n```json\n{raw}\n```"
        model = OpenRouterModel(api_key="fixture-key", tool_schemas={}, transport=CaptureTransport(response))

        with self.assertRaises(OpenRouterError):
            model.respond(goal="Verify the cart total", observations=())


def blocked_trace(*, reason, artifact=b"HTTP status: 503 Service Unavailable\nCart service unavailable"):
    return EvaluationTrace(
        execution_id="run-1",
        execution=Execution(findings=(Finding(
            expected="Cart total is $20", observed="Cart service unavailable", result=FindingResult.INCONCLUSIVE,
            evidence=("cart.txt",), reason=reason,
        ),), goal_verified=False),
        tool_events=(ToolEvent(name="browser_snapshot", authorized=True, executed=True),),
        artifacts={"cart.txt": artifact},
        instrumentation_complete=True,
    )


class BlockedScenarioRegressionTests(unittest.TestCase):
    def test_controlled_blocked_scenario_requires_the_service_failure_evidence(self):
        scenarios = build_scenarios("http://127.0.0.1:43500")
        blocked = next(item.case for item in scenarios if item.case.expected_result == FindingResult.INCONCLUSIVE)

        self.assertIn("503 Service Unavailable", blocked.required_evidence_text)

    def test_provider_error_is_not_a_passing_blocked_scenario(self):
        case = EvaluationCase(name="blocked", goal="Verify cart", expected_result=FindingResult.INCONCLUSIVE)
        trace = blocked_trace(reason="Model error: Invalid structured final answer")

        report = EvaluationRunner(execute=lambda scenario: trace).evaluate((case,))

        self.assertFalse(report.passed)
        self.assertIn("model", " ".join(report.cases[0].issues).lower())

    def test_tool_disconnection_is_not_a_passing_blocked_scenario(self):
        case = EvaluationCase(name="blocked", goal="Verify cart", expected_result=FindingResult.INCONCLUSIVE)
        trace = blocked_trace(reason="Tool error: browser disconnected")

        report = EvaluationRunner(execute=lambda scenario: trace).evaluate((case,))

        self.assertFalse(report.passed)
        self.assertIn("tool", " ".join(report.cases[0].issues).lower())

    def test_blocked_fixture_requires_its_expected_service_observation(self):
        case = EvaluationCase(
            name="blocked", goal="Verify cart", expected_result=FindingResult.INCONCLUSIVE,
            required_evidence_text=("503 Service Unavailable",),
        )
        trace = blocked_trace(reason="Cart service unavailable", artifact=b"A different login page was unavailable")

        report = EvaluationRunner(execute=lambda scenario: trace).evaluate((case,))

        self.assertFalse(report.passed)
        self.assertIn("evidence", " ".join(report.cases[0].issues).lower())

    def test_expected_service_observation_with_inconclusive_result_passes(self):
        case = EvaluationCase(
            name="blocked", goal="Verify cart", expected_result=FindingResult.INCONCLUSIVE,
            required_evidence_text=("503 Service Unavailable",),
        )
        trace = blocked_trace(reason="Cart service unavailable")

        report = EvaluationRunner(execute=lambda scenario: trace).evaluate((case,))

        self.assertTrue(report.passed)


if __name__ == "__main__":
    unittest.main()
