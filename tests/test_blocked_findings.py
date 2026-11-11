"""Proposed regressions for empty or unsupported blocked-scenario findings."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scrappy_doo.cli import LocalRuntime
from scrappy_doo.evaluation import EvaluationCase, EvaluationRunner
from scrappy_doo.execution import Execution, Finding, FindingResult
from scrappy_doo.openrouter import OpenRouterModel
from scrappy_doo.orchestration import FinalAnswer, FindingUpdate, Observation, Tool, ToolCall
from scrappy_doo.storage import ExecutionStore
from scrappy_doo.tracing import TraceRecorder


REFERENCE = "observations/0001.txt"
BLOCKED_PAGE = "HTTP status: 503 Service Unavailable\nCart service unavailable"


def supported_finding():
    return Finding(
        expected="Cart total is $20", observed="Cart service unavailable (HTTP 503)",
        result=FindingResult.INCONCLUSIVE, evidence=(REFERENCE,),
        reason="The cart service is unavailable, so the total could not be checked.",
    )


class BrowserDouble:
    def __init__(self, *, config):
        self.calls = []

    def start(self):
        pass

    def close(self):
        pass

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        return "Cart page loaded"

    def begin_manual_sign_in(self):
        pass

    def finish_manual_sign_in(self):
        pass

    def as_tools(self):
        def snapshot():
            self.calls.append(("browser_snapshot", {}))
            return BLOCKED_PAGE
        return {"browser_snapshot": Tool(handler=snapshot)}


class ModelDouble:
    """Model boundary double supporting one corrective response."""

    def __init__(self, responses, correction):
        self.responses = iter(responses)
        self.correction = correction
        self.corrections = []

    def respond(self, *, goal, observations):
        return next(self.responses)

    def correct(self, *, goal, observations, feedback):
        self.corrections.append((goal, observations, feedback))
        if isinstance(self.correction, Exception):
            raise self.correction
        return self.correction


class BlockedFindingRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data_dir = Path(temporary.name) / "data"
        self.store = ExecutionStore(self.data_dir)
        self.execution_id = self.store.save_execution(
            application="shop", environment="staging", execution=Execution(findings=(), goal_verified=False),
        )
        self.trace = TraceRecorder(store=self.store, execution_id=self.execution_id)

    def run_case(self, initial, correction, *, capture_text=True, middle=()):
        model = ModelDouble(
            [ToolCall(name="browser_snapshot", arguments={}), *middle, initial], correction,
        )
        browser = []

        def browser_factory(*, config):
            instance = BrowserDouble(config=config)
            browser.append(instance)
            return instance

        runtime = LocalRuntime(browser_factory=browser_factory, model_factory=lambda **kwargs: model)
        environment = {
            "url": "https://shop.example/cart", "allowed_origins": ["https://shop.example"],
            "capture_screenshots": False, "send_screenshots": False, "local_roots": [],
            "capture_history": False, "capture_text": capture_text, "allow_changes": False,
            "model": "z-ai/glm-5.3-flash", "max_tool_calls": 5, "timeout_seconds": 30,
            "approved_knowledge": (), "_trace": self.trace,
        }
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "fixture-key"}), patch("shutil.which", return_value="npx"):
            runtime.validate(application="shop", environment=environment)
            try:
                execution = runtime.run(
                    application="shop", environment=environment, goal="Verify cart total",
                    approve=lambda call: False, manual_sign_in=lambda: True,
                )
            finally:
                runtime.close()
        return execution, runtime, model, browser[0]

    def evaluate(self, execution, runtime):
        for name, content in runtime.artifacts.items():
            self.store.save_artifact(self.execution_id, name, content)
        self.trace.finish(execution)
        trace = self.store.evaluation_trace(self.execution_id)
        return EvaluationRunner(execute=lambda case: trace).evaluate((EvaluationCase(
            name="blocked", goal="Verify cart total", expected_result=FindingResult.INCONCLUSIVE,
            required_tools=("browser_snapshot",), required_evidence_text=("503 Service Unavailable",),
        ),))

    def test_empty_final_after_503_is_corrected_with_a_real_evidence_reference(self):
        execution, runtime, model, _ = self.run_case(
            FinalAnswer(findings=(), goal_verified=False),
            FinalAnswer(findings=(supported_finding(),), goal_verified=False),
        )

        self.assertEqual(len(model.corrections), 1)
        self.assertIn(REFERENCE, model.corrections[0][2])
        self.assertEqual(execution.overall_result, FindingResult.INCONCLUSIVE)
        self.assertEqual(execution.findings[0].evidence, (REFERENCE,))
        self.assertEqual(runtime.artifacts[REFERENCE], BLOCKED_PAGE.encode())

    def test_corrected_blocked_finding_and_saved_trace_pass_the_controlled_case(self):
        execution, runtime, _, _ = self.run_case(
            FinalAnswer(findings=(), goal_verified=False),
            FinalAnswer(findings=(supported_finding(),), goal_verified=False),
        )

        report = self.evaluate(execution, runtime)

        self.assertTrue(report.passed, report.format())

    def test_inconclusive_finding_with_unknown_evidence_is_corrected(self):
        unsupported = Finding(
            expected="Cart total is $20", observed="Cart service unavailable (HTTP 503)",
            result=FindingResult.INCONCLUSIVE, evidence=("not-captured.txt",), reason="Service unavailable",
        )
        execution, _, model, _ = self.run_case(
            FinalAnswer(findings=(unsupported,), goal_verified=False),
            FinalAnswer(findings=(supported_finding(),), goal_verified=False),
        )

        self.assertEqual(len(model.corrections), 1)
        self.assertEqual(execution.findings[0].evidence, (REFERENCE,))
        self.assertNotIn("not-captured.txt", execution.findings[0].evidence)

    def test_correction_does_not_repeat_the_browser_action(self):
        _, _, _, browser = self.run_case(
            FinalAnswer(findings=(), goal_verified=False),
            FinalAnswer(findings=(supported_finding(),), goal_verified=False),
        )

        self.assertEqual([name for name, _ in browser.calls], ["browser_navigate", "browser_snapshot"])

    def test_repeated_empty_response_produces_diagnostic_with_captured_evidence(self):
        execution, runtime, model, _ = self.run_case(
            FinalAnswer(findings=(), goal_verified=False), FinalAnswer(findings=(), goal_verified=False),
        )

        self.assertEqual(len(model.corrections), 1)
        self.assertEqual(execution.overall_result, FindingResult.INCONCLUSIVE)
        self.assertEqual(execution.findings[0].evidence, (REFERENCE,))
        self.assertIn("503 Service Unavailable", execution.findings[0].observed)
        self.assertIn("model error", execution.findings[0].reason.lower())
        self.assertFalse(self.evaluate(execution, runtime).passed)

    def test_empty_answer_cannot_pass_merely_by_marking_goal_verified(self):
        execution, _, model, _ = self.run_case(
            FinalAnswer(findings=(), goal_verified=True),
            FinalAnswer(findings=(supported_finding(),), goal_verified=False),
        )

        self.assertEqual(len(model.corrections), 1)
        self.assertEqual(execution.overall_result, FindingResult.INCONCLUSIVE)

    def test_correction_failure_preserves_evidence_but_does_not_pass_the_case(self):
        execution, runtime, model, _ = self.run_case(
            FinalAnswer(findings=(), goal_verified=False), OSError("provider unavailable"),
        )

        self.assertEqual(len(model.corrections), 1)
        self.assertEqual(execution.findings[0].evidence, (REFERENCE,))
        self.assertIn("model error", execution.findings[0].reason.lower())
        self.assertFalse(self.evaluate(execution, runtime).passed)

    def test_second_unsupported_reference_is_not_presented_as_captured_evidence(self):
        invalid = Finding(
            expected="Cart total is $20", observed="Cart service unavailable (HTTP 503)",
            result=FindingResult.INCONCLUSIVE, evidence=("invented.txt",), reason="Service unavailable",
        )
        execution, runtime, model, _ = self.run_case(
            FinalAnswer(findings=(), goal_verified=False), FinalAnswer(findings=(invalid,), goal_verified=False),
        )

        self.assertEqual(len(model.corrections), 1)
        self.assertNotIn("invented.txt", execution.findings[0].evidence)
        self.assertIn("model error", execution.findings[0].reason.lower())
        self.assertFalse(self.evaluate(execution, runtime).passed)

    def test_disabled_capture_does_not_invent_an_evidence_reference(self):
        execution, runtime, model, _ = self.run_case(
            FinalAnswer(findings=(), goal_verified=False), FinalAnswer(findings=(), goal_verified=False),
            capture_text=False,
        )

        self.assertEqual(runtime.artifacts, {})
        self.assertEqual(len(model.corrections), 1)
        self.assertEqual(execution.findings[0].evidence, ())
        self.assertIn("model error", execution.findings[0].reason.lower())
        self.assertFalse(self.evaluate(execution, runtime).passed)

    def test_disabled_capture_does_not_trust_a_model_invented_reference(self):
        unsupported = Finding(
            expected="Cart total is $20", observed="Cart unavailable", result=FindingResult.INCONCLUSIVE,
            evidence=(REFERENCE,), reason="Service unavailable",
        )
        execution, runtime, model, _ = self.run_case(
            FinalAnswer(findings=(unsupported,), goal_verified=False),
            FinalAnswer(findings=(unsupported,), goal_verified=False), capture_text=False,
        )

        self.assertEqual(runtime.artifacts, {})
        self.assertEqual(len(model.corrections), 1)
        self.assertEqual(execution.findings[0].evidence, ())
        self.assertIn("model error", execution.findings[0].reason.lower())

    def test_supported_finding_does_not_request_a_correction(self):
        execution, _, model, _ = self.run_case(
            FinalAnswer(findings=(supported_finding(),), goal_verified=False),
            FinalAnswer(findings=(), goal_verified=False),
        )

        self.assertEqual(model.corrections, [])
        self.assertEqual(execution.findings[0].evidence, (REFERENCE,))

    def test_supported_partial_finding_is_not_lost_when_final_has_no_findings(self):
        execution, _, model, _ = self.run_case(
            FinalAnswer(findings=(), goal_verified=False), FinalAnswer(findings=(), goal_verified=False),
            middle=(FindingUpdate(finding=supported_finding()),),
        )

        self.assertEqual(model.corrections, [])
        self.assertEqual(execution.findings[0].evidence, (REFERENCE,))


def provider_response(*, tool=False, findings=()):
    if tool:
        message = {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call-1", "type": "function", "function": {"name": "browser_snapshot", "arguments": "{}"},
        }]}
        finish = "tool_calls"
    else:
        message = {"role": "assistant", "content": json.dumps({
            "goal_verified": False, "findings": list(findings), "knowledge_proposals": [],
        })}
        finish = "stop"
    return {"choices": [{"finish_reason": finish, "message": message}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0}}


class RecordingTransport:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    def __call__(self, *, url, headers, body):
        self.requests.append(body)
        return next(self.responses)


class ProviderCorrectionTests(unittest.TestCase):
    def test_structured_schema_requests_at_least_one_finding(self):
        transport = RecordingTransport(provider_response(tool=True))
        model = OpenRouterModel(
            api_key="fixture-key", transport=transport,
            tool_schemas={"browser_snapshot": {"type": "object", "properties": {}, "required": []}},
        )

        model.respond(goal="Verify cart total", observations=())

        schema = transport.requests[0]["response_format"]["json_schema"]["schema"]
        self.assertGreaterEqual(schema["properties"]["findings"]["minItems"], 1)

    def test_correction_preserves_tool_context_and_sends_feedback_as_a_separate_message(self):
        finding = {
            "expected": "Cart total is $20", "observed": "Cart service unavailable (HTTP 503)",
            "result": "inconclusive", "evidence": [REFERENCE], "reason": "Service unavailable",
        }
        transport = RecordingTransport(provider_response(tool=True), provider_response(), provider_response(findings=(finding,)))
        model = OpenRouterModel(
            api_key="fixture-key", transport=transport,
            tool_schemas={"browser_snapshot": {"type": "object", "properties": {}, "required": []}},
            required_initial_tool="browser_snapshot",
        )
        model.respond(goal="Verify cart total", observations=())
        observations = (Observation(content=f"Evidence reference: {REFERENCE}\n{BLOCKED_PAGE}"),)
        model.respond(goal="Verify cart total", observations=observations)

        answer = model.correct(goal="Verify cart total", observations=observations, feedback=f"Return a finding citing {REFERENCE}.")

        messages = transport.requests[2]["messages"]
        self.assertTrue(any(message.get("role") == "tool" and message.get("tool_call_id") == "call-1" for message in messages))
        self.assertEqual(messages[-1]["role"], "user")
        self.assertIn(REFERENCE, messages[-1]["content"])
        self.assertEqual(answer.findings[0].evidence, (REFERENCE,))


if __name__ == "__main__":
    unittest.main()
