"""Proposed regressions for live runs that answer before inspecting the page."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scrappy_doo.cli import LocalRuntime
from scrappy_doo.execution import Execution
from scrappy_doo.openrouter import OpenRouterError, OpenRouterModel
from scrappy_doo.orchestration import FinalAnswer, Observation, Tool
from scrappy_doo.storage import ExecutionStore
from scrappy_doo.tracing import TraceRecorder


SCHEMAS = {
    "browser_snapshot": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    "browser_navigate": {
        "type": "object", "properties": {"url": {"type": "string"}},
        "required": ["url"], "additionalProperties": False,
    },
}


def tool_response(name="browser_snapshot", arguments="{}"):
    return {
        "choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "call-1", "type": "function", "function": {
                "name": name, "arguments": arguments,
            }}],
        }}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0},
    }


def final_response():
    return {
        "choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": json.dumps({
                "goal_verified": False, "findings": [], "knowledge_proposals": [],
            }),
        }}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0},
    }


class RecordingTransport:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    def __call__(self, *, url, headers, body):
        self.requests.append(body)
        return next(self.responses)


class InitialInspectionTests(unittest.TestCase):
    def test_first_request_explicitly_selects_the_snapshot_tool(self):
        transport = RecordingTransport(tool_response())
        model = OpenRouterModel(
            api_key="fixture-key", transport=transport, tool_schemas=SCHEMAS,
            required_initial_tool="browser_snapshot",
        )

        result = model.respond(goal="Verify cart total", observations=())

        self.assertEqual(result.name, "browser_snapshot")
        self.assertEqual(transport.requests[0]["tool_choice"], {
            "type": "function", "function": {"name": "browser_snapshot"},
        })

    def test_after_initial_observation_model_can_choose_its_next_response(self):
        transport = RecordingTransport(tool_response(), final_response())
        model = OpenRouterModel(
            api_key="fixture-key", transport=transport, tool_schemas=SCHEMAS,
            required_initial_tool="browser_snapshot",
        )
        model.respond(goal="Verify cart total", observations=())

        result = model.respond(goal="Verify cart total", observations=(Observation(content="Cart total: $20"),))

        self.assertIsInstance(result, FinalAnswer)
        self.assertEqual(transport.requests[1].get("tool_choice", "auto"), "auto")

    def test_final_answer_before_required_inspection_is_rejected(self):
        model = OpenRouterModel(
            api_key="fixture-key", transport=RecordingTransport(final_response()), tool_schemas=SCHEMAS,
            required_initial_tool="browser_snapshot",
        )

        with self.assertRaises(OpenRouterError):
            model.respond(goal="Verify cart total", observations=())

    def test_different_initial_tool_is_rejected_when_snapshot_was_required(self):
        response = tool_response(name="browser_navigate", arguments='{"url":"https://example.com"}')
        model = OpenRouterModel(
            api_key="fixture-key", transport=RecordingTransport(response), tool_schemas=SCHEMAS,
            required_initial_tool="browser_snapshot",
        )

        with self.assertRaises(OpenRouterError):
            model.respond(goal="Verify cart total", observations=())

    def test_requiring_an_unavailable_tool_fails_before_a_request(self):
        transport = RecordingTransport()

        with self.assertRaises(OpenRouterError):
            OpenRouterModel(
                api_key="fixture-key", transport=transport, tool_schemas={},
                required_initial_tool="browser_snapshot",
            )

        self.assertEqual(transport.requests, [])

    def test_runtime_requires_initial_snapshot_only_after_manual_sign_in(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = ExecutionStore(Path(temporary.name) / "data")
        execution_id = store.save_execution(
            application="shop", environment="staging", execution=Execution(findings=(), goal_verified=False),
        )
        events, options = [], {}

        class BrowserDouble:
            def __init__(self, *, config):
                pass

            def start(self):
                pass

            def close(self):
                pass

            def call(self, name, arguments):
                return "Cart loaded"

            def begin_manual_sign_in(self):
                events.append("sign-in-started")

            def finish_manual_sign_in(self):
                events.append("sign-in-finished")

            def as_tools(self):
                return {"browser_snapshot": Tool(handler=lambda: "Cart total: $20")}

        class ModelDouble:
            def __init__(self, **kwargs):
                options.update(kwargs)

            def respond(self, *, goal, observations):
                events.append("model-requested")
                return FinalAnswer(findings=(), goal_verified=False)

        runtime = LocalRuntime(browser_factory=BrowserDouble, model_factory=ModelDouble)
        environment = {
            "url": "https://shop.example", "allowed_origins": ["https://shop.example"],
            "capture_screenshots": False, "send_screenshots": False, "local_roots": [],
            "capture_history": False, "capture_text": False, "allow_changes": False,
            "model": "z-ai/glm-5.3-flash", "max_tool_calls": 5, "timeout_seconds": 30,
            "approved_knowledge": (), "_trace": TraceRecorder(store=store, execution_id=execution_id),
        }
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "fixture-key"}), patch("shutil.which", return_value="npx"):
            runtime.validate(application="shop", environment=environment)
            runtime.run(application="shop", environment=environment, goal="Verify cart total",
                        approve=lambda call: False, manual_sign_in=lambda: True)
            runtime.close()

        self.assertEqual(options["required_initial_tool"], "browser_snapshot")
        self.assertLess(events.index("sign-in-finished"), events.index("model-requested"))


if __name__ == "__main__":
    unittest.main()
