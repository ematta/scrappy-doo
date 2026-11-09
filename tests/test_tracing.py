"""Proposed durable runtime tracing contract for coverage review.

Tool events are recorded by the execution boundary, not returned by the model.
These tests do not claim protection against a compromised host or database.
"""

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scrappy_doo.cli import LocalRuntime, main
from scrappy_doo.evaluation import EvaluationCase, EvaluationRunner
from scrappy_doo.execution import Execution, ExecutionStatus, Finding, FindingResult
from scrappy_doo.knowledge import KnowledgeStore
from scrappy_doo.orchestration import ExecutionRunner, FinalAnswer, RunPolicy, Tool, ToolCall
from scrappy_doo.storage import ExecutionStore
from scrappy_doo.tracing import TraceRecorder


class ScriptedModel:
    def __init__(self, *responses):
        self.responses = iter(responses)

    def respond(self, *, goal, observations):
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response


def final_answer():
    return FinalAnswer(findings=(Finding(
        expected="Cart total is $20", observed="Cart total is $20",
        result=FindingResult.PASSED, evidence=("cart.txt",),
    ),), goal_verified=True)


class TraceRecorderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data_dir = Path(temporary.name) / "data"
        self.store = ExecutionStore(self.data_dir)
        self.execution_id = self.store.save_execution(
            application="shop", environment="staging", execution=Execution(findings=(), goal_verified=False),
        )
        self.recorder = TraceRecorder(store=self.store, execution_id=self.execution_id)

    def run_agent(self, model, tools, *, allowed=(), prohibited=(), approve=None):
        runner = ExecutionRunner(model=model, tools=tools, approve=approve, trace=self.recorder)
        return runner.run(goal="Verify cart total", policy=RunPolicy(
            allowed_tools=frozenset(allowed), prohibited_tools=frozenset(prohibited), max_tool_calls=5,
        ))

    def test_new_trace_is_incomplete_until_execution_is_finalized(self):
        trace = self.recorder.snapshot()

        self.assertFalse(trace.instrumentation_complete)
        self.assertEqual(trace.tool_events, ())

    def test_authorized_tool_event_is_recorded_before_handler_runs(self):
        observed_during_call = []

        def snapshot():
            observed_during_call.extend(TraceRecorder(store=self.store, execution_id=self.execution_id).snapshot().tool_events)
            return "Cart total: $20"

        model = ScriptedModel(ToolCall(name="browser_snapshot", arguments={}), final_answer())
        execution = self.run_agent(model, {"browser_snapshot": Tool(handler=snapshot)}, allowed=("browser_snapshot",))

        self.assertEqual(execution.overall_result, FindingResult.PASSED)
        self.assertEqual(len(observed_during_call), 1)
        self.assertEqual(observed_during_call[0].name, "browser_snapshot")
        self.assertTrue(observed_during_call[0].authorized)
        self.assertTrue(observed_during_call[0].executed)

    def test_multiple_tool_events_survive_reopening_in_order(self):
        tools = {"first": Tool(handler=lambda: "one"), "second": Tool(handler=lambda: "two")}
        model = ScriptedModel(ToolCall(name="first", arguments={}), ToolCall(name="second", arguments={}), final_answer())

        self.run_agent(model, tools, allowed=("first", "second"))
        reopened = TraceRecorder(store=ExecutionStore(self.data_dir), execution_id=self.execution_id).snapshot()

        self.assertEqual([event.name for event in reopened.tool_events], ["first", "second"])
        self.assertTrue(all(event.authorized and event.executed for event in reopened.tool_events))
        self.assertFalse(reopened.instrumentation_complete)

    def test_declined_approval_is_recorded_without_executing_the_tool(self):
        calls = []
        model = ScriptedModel(ToolCall(name="delete_cart", arguments={}))
        execution = self.run_agent(model, {"delete_cart": Tool(handler=lambda: calls.append("deleted"))}, approve=lambda call: False)

        event = self.recorder.snapshot().tool_events[0]
        self.assertEqual(execution.overall_result, FindingResult.INCONCLUSIVE)
        self.assertEqual(calls, [])
        self.assertEqual(event.name, "delete_cart")
        self.assertFalse(event.authorized)
        self.assertFalse(event.executed)

    def test_action_specific_approval_is_recorded_as_authorized(self):
        model = ScriptedModel(ToolCall(name="delete_cart", arguments={}), final_answer())

        self.run_agent(model, {"delete_cart": Tool(handler=lambda: "deleted")}, approve=lambda call: True)

        event = self.recorder.snapshot().tool_events[0]
        self.assertTrue(event.authorized)
        self.assertTrue(event.executed)

    def test_environment_prohibition_is_recorded_without_invoking_approval_or_tool(self):
        calls, approvals = [], []
        model = ScriptedModel(ToolCall(name="delete_cart", arguments={}))

        self.run_agent(
            model, {"delete_cart": Tool(handler=lambda: calls.append("deleted"))},
            prohibited=("delete_cart",), approve=lambda call: approvals.append(call) or True,
        )

        event = self.recorder.snapshot().tool_events[0]
        self.assertFalse(event.authorized)
        self.assertFalse(event.executed)
        self.assertEqual(calls, [])
        self.assertEqual(approvals, [])

    def test_unknown_tool_request_is_recorded_as_unexecuted(self):
        model = ScriptedModel(ToolCall(name="run_shell", arguments={}))

        self.run_agent(model, {})

        event = self.recorder.snapshot().tool_events[0]
        self.assertEqual(event.name, "run_shell")
        self.assertFalse(event.authorized)
        self.assertFalse(event.executed)

    def test_invalid_arguments_are_recorded_as_unexecuted(self):
        calls = []
        model = ScriptedModel(ToolCall(name="browser_navigate", arguments={}))

        self.run_agent(model, {"browser_navigate": Tool(handler=lambda **kwargs: calls.append(kwargs), required_arguments=("url",))}, allowed=("browser_navigate",))

        event = self.recorder.snapshot().tool_events[0]
        self.assertFalse(event.executed)
        self.assertEqual(calls, [])

    def test_trace_does_not_persist_tool_arguments_or_tool_output(self):
        secret = "fictional-private-token"
        model = ScriptedModel(ToolCall(name="inspect", arguments={"target": secret}), final_answer())

        self.run_agent(model, {"inspect": Tool(handler=lambda target: secret, required_arguments=("target",))}, allowed=("inspect",))

        events = TraceRecorder(store=ExecutionStore(self.data_dir), execution_id=self.execution_id).snapshot().tool_events
        self.assertEqual(events[0].name, "inspect")
        self.assertNotIn(secret, repr(events))
        self.assertNotIn(secret.encode(), (self.data_dir / "executions.sqlite3").read_bytes())

    def test_handler_failure_keeps_the_started_tool_event(self):
        def disconnected():
            raise OSError("browser disconnected")

        execution = self.run_agent(
            ScriptedModel(ToolCall(name="browser_snapshot", arguments={})),
            {"browser_snapshot": Tool(handler=disconnected)}, allowed=("browser_snapshot",),
        )

        event = self.recorder.snapshot().tool_events[0]
        self.assertTrue(event.authorized)
        self.assertTrue(event.executed)
        self.assertEqual(execution.overall_result, FindingResult.INCONCLUSIVE)

    def test_interruption_preserves_started_event_and_does_not_mark_trace_complete(self):
        def interrupted():
            raise KeyboardInterrupt()

        execution = self.run_agent(
            ScriptedModel(ToolCall(name="browser_snapshot", arguments={})),
            {"browser_snapshot": Tool(handler=interrupted)}, allowed=("browser_snapshot",),
        )

        trace = TraceRecorder(store=ExecutionStore(self.data_dir), execution_id=self.execution_id).snapshot()
        self.assertEqual(execution.status, ExecutionStatus.INTERRUPTED)
        self.assertTrue(trace.tool_events[0].executed)
        self.assertFalse(trace.instrumentation_complete)

    def test_trace_write_failure_stops_before_tool_execution(self):
        calls = []
        model = ScriptedModel(ToolCall(name="browser_snapshot", arguments={}))
        with patch("sqlite3.connect", side_effect=OSError("trace storage unavailable")):
            execution = self.run_agent(
                model, {"browser_snapshot": Tool(handler=lambda: calls.append("ran"))},
                allowed=("browser_snapshot",),
            )

        self.assertEqual(calls, [])
        self.assertEqual(execution.overall_result, FindingResult.INCONCLUSIVE)
        self.assertFalse(self.recorder.snapshot().instrumentation_complete)

    def test_finishing_trace_persists_execution_and_marks_it_complete(self):
        self.store.save_artifact(self.execution_id, "cart.txt", b"Cart total: $20")
        execution = self.run_agent(
            ScriptedModel(ToolCall(name="browser_snapshot", arguments={}), final_answer()),
            {"browser_snapshot": Tool(handler=lambda: "Cart total: $20")}, allowed=("browser_snapshot",),
        )

        self.recorder.finish(execution)
        trace = TraceRecorder(store=ExecutionStore(self.data_dir), execution_id=self.execution_id).snapshot()

        self.assertTrue(trace.instrumentation_complete)
        self.assertEqual(trace.execution.overall_result, FindingResult.PASSED)
        self.assertEqual(trace.artifacts["cart.txt"], b"Cart total: $20")
        self.assertEqual(len(trace.tool_events), 1)

    def test_older_uninstrumented_execution_is_not_reported_as_complete(self):
        trace = self.store.evaluation_trace(self.execution_id)

        self.assertFalse(trace.instrumentation_complete)

    def test_snapshot_uses_actual_stored_knowledge_and_pending_proposals(self):
        self.store.save_artifact(self.execution_id, "cart.txt", b"Cart total: $20")
        knowledge = KnowledgeStore(self.data_dir)
        approved_id = knowledge.propose(
            application="shop", environment="staging", text="Cart total is visible",
            source_execution_id=self.execution_id, evidence=("cart.txt",),
        )
        knowledge.approve(approved_id)
        proposal_id = knowledge.propose(
            application="shop", environment="staging", text="Cart is in the menu",
            source_execution_id=self.execution_id, evidence=("cart.txt",),
        )
        self.store.record_knowledge_used(self.execution_id, (approved_id,))

        trace = self.recorder.snapshot()

        self.assertEqual(trace.knowledge_used, (approved_id,))
        self.assertEqual([item.id for item in trace.proposals], [proposal_id])
        self.assertEqual(trace.proposals[0].source_execution_id, self.execution_id)


class CLIEvaluationTraceTests(unittest.TestCase):
    def test_run_with_history_capture_disabled_produces_evaluable_trace(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        data_dir = Path(temporary.name) / "data"
        events_during_navigation = []

        class BrowserDouble:
            def __init__(self, *, config):
                pass

            def start(self):
                pass

            def close(self):
                pass

            def call(self, name, arguments):
                store = ExecutionStore(data_dir)
                run_ids = store.list_executions(application="shop", environment="staging")
                if run_ids:
                    events_during_navigation.extend(store.evaluation_trace(run_ids[-1]).tool_events)
                return "Cart page loaded"

            def begin_manual_sign_in(self):
                pass

            def finish_manual_sign_in(self):
                pass

            def as_tools(self):
                return {"browser_snapshot": Tool(handler=lambda: "Cart total: $20")}

        class ModelDouble:
            def __init__(self, *, tool_schemas, model, approved_knowledge):
                self.turn = 0

            def respond(self, *, goal, observations):
                self.turn += 1
                if self.turn == 1:
                    return ToolCall(name="browser_snapshot", arguments={})
                return FinalAnswer(findings=(Finding(
                    expected="Cart total is $20", observed="Cart total is $20",
                    result=FindingResult.PASSED, evidence=("observations/0001.txt",),
                ),), goal_verified=True)

        def invoke(*args):
            output, error = io.StringIO(), io.StringIO()
            result = main(
                ["--data-dir", str(data_dir), *args], input_fn=lambda prompt: "yes",
                stdout=output, stderr=error, runtime=LocalRuntime(browser_factory=BrowserDouble, model_factory=ModelDouble),
            )
            return result, error.getvalue()

        invoke(
            "environment", "add", "--application", "shop", "--name", "staging",
            "--url", "https://shop.example", "--allow-origin", "https://shop.example",
            "--boundary", "best-effort", "--no-history-capture",
        )
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "fictional-key"}), patch("shutil.which", return_value="npx"):
            code, error = invoke("run", "--application", "shop", "--environment", "staging", "--goal", "Verify cart total")

        self.assertEqual(code, 0, error)
        store = ExecutionStore(data_dir)
        run_id = store.list_executions(application="shop", environment="staging")[0]
        self.assertEqual(store.read_history(run_id), [])
        trace = store.evaluation_trace(run_id)
        report = EvaluationRunner(execute=lambda scenario: trace).evaluate((EvaluationCase(
            name="cart", goal="Verify cart total", expected_result=FindingResult.PASSED,
            required_tools=("browser_snapshot",),
        ),))
        self.assertTrue(trace.instrumentation_complete)
        self.assertEqual(events_during_navigation[0].name, "browser_navigate")
        self.assertTrue(events_during_navigation[0].authorized)
        self.assertTrue(events_during_navigation[0].executed)
        self.assertEqual([event.name for event in trace.tool_events], ["browser_navigate", "browser_snapshot"])
        self.assertEqual(trace.artifacts["observations/0001.txt"], b"Cart total: $20")
        self.assertTrue(report.passed, report.format())


if __name__ == "__main__":
    unittest.main()
