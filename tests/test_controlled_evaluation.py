"""Proposed controlled-sample and evaluation-driver contract.

These are unit tests: the sample is exercised in process, and the server and
agent boundaries are doubles. They do not launch a browser or call a provider.
"""

import json
import tempfile
import unittest
from pathlib import Path

from scrappy_doo.controlled_evaluation import build_scenarios, run_controlled_evaluation
from scrappy_doo.execution import Execution, Finding, FindingResult
from scrappy_doo.sample_app import SampleApplication
from scrappy_doo.storage import ExecutionStore
from scrappy_doo.tracing import TraceRecorder


class SampleApplicationTests(unittest.TestCase):
    def setUp(self):
        self.app = SampleApplication()

    def request(self, method, path, *, cookie=None, body=b""):
        headers = {"Cookie": cookie} if cookie else {}
        return self.app.handle(method=method, path=path, headers=headers, body=body)

    def login(self):
        response = self.request(
            "POST", "/login", body=json.dumps({"username": "qa", "password": "test-password"}).encode(),
        )
        self.assertEqual(response.status, 200)
        return response.headers["Set-Cookie"].split(";", 1)[0]

    def test_login_page_is_available_without_a_session(self):
        response = self.request("GET", "/login")

        self.assertEqual(response.status, 200)
        self.assertIn(b"login", response.body.lower())

    def test_cart_requires_authentication(self):
        response = self.request("GET", "/cart/a")

        self.assertEqual(response.status, 401)
        self.assertNotIn(b"$20", response.body)

    def test_invalid_credentials_do_not_create_a_session(self):
        response = self.request(
            "POST", "/login", body=json.dumps({"username": "qa", "password": "wrong"}).encode(),
        )

        self.assertEqual(response.status, 401)
        self.assertNotIn("Set-Cookie", response.headers)

    def test_successful_login_returns_a_session_cookie(self):
        cookie = self.login()

        self.assertTrue(cookie)
        self.assertNotIn("test-password", cookie)

    def test_login_accepts_the_browser_form_encoding(self):
        response = self.app.handle(
            method="POST", path="/login",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=b"username=qa&password=test-password",
        )

        self.assertEqual(response.status, 200)
        self.assertIn("Set-Cookie", response.headers)

    def test_working_cart_displays_the_known_correct_total(self):
        response = self.request("GET", "/cart/a", cookie=self.login())

        self.assertEqual(response.status, 200)
        self.assertIn(b"$20", response.body)

    def test_faulty_cart_displays_the_known_incorrect_total(self):
        response = self.request("GET", "/cart/b", cookie=self.login())

        self.assertEqual(response.status, 200)
        self.assertIn(b"$25", response.body)

    def test_unavailable_cart_is_a_blocker_without_a_total(self):
        response = self.request("GET", "/cart/c", cookie=self.login())

        self.assertEqual(response.status, 503)
        self.assertNotIn(b"$20", response.body)

    def test_reset_invalidates_existing_sessions(self):
        cookie = self.login()
        self.app.reset()

        self.assertEqual(self.request("GET", "/cart/a", cookie=cookie).status, 401)

    def test_unknown_routes_do_not_return_a_cart(self):
        response = self.request("GET", "/not-a-page", cookie=self.login())

        self.assertEqual(response.status, 404)


class FakeSampleServer:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.reset_count = 0
        self.base_url = "http://127.0.0.1:43500"

    def start(self):
        self.started = True
        return self.base_url

    def reset(self):
        self.reset_count += 1

    def stop(self):
        self.stopped = True


class FakeAgentBoundary:
    """Create durable, instrumented runs without a real model or browser."""

    def __init__(self, store, *, failure_on=None):
        self.store = store
        self.failure_on = failure_on
        self.validation_error = None
        self.result_override = None
        self.calls = []

    def validate(self):
        if self.validation_error:
            raise self.validation_error

    def run(self, *, start_url, goal, manual_login, approve_action):
        self.calls.append((start_url, goal, manual_login, approve_action))
        if start_url.endswith(self.failure_on or "\0"):
            raise OSError("provider unavailable")
        result = {
            "/cart/a": FindingResult.PASSED,
            "/cart/b": FindingResult.FAILED,
            "/cart/c": FindingResult.INCONCLUSIVE,
        }["/" + start_url.split("/", 3)[-1]]
        if self.result_override is not None:
            result = self.result_override
        reference = "observations/cart.txt"
        execution = Execution(
            findings=(Finding(
                expected="Cart total is $20", observed="Cart observation",
                result=result, evidence=() if result == FindingResult.INCONCLUSIVE else (reference,),
                reason="Service unavailable" if result == FindingResult.INCONCLUSIVE else None,
            ),),
            goal_verified=result == FindingResult.PASSED,
        )
        execution_id = self.store.save_execution(application="controlled-sample", environment="local", execution=execution)
        recorder = TraceRecorder(store=self.store, execution_id=execution_id)
        recorder.activate()
        event = recorder.request_tool("browser_snapshot")
        recorder.start_tool(event)
        if result != FindingResult.INCONCLUSIVE:
            self.store.save_artifact(execution_id, reference, b"Cart observation")
        recorder.finish(execution)
        return execution_id


class ControlledEvaluationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = ExecutionStore(Path(temporary.name) / "data")
        self.server = FakeSampleServer()
        self.agent = FakeAgentBoundary(self.store)
        self.manual_login = lambda: True
        self.approve_action = lambda call: False

    def run_evaluation(self):
        return run_controlled_evaluation(
            server=self.server, agent=self.agent, store=self.store,
            manual_login=self.manual_login, approve_action=self.approve_action,
        )

    def test_scenarios_define_pass_fail_and_blocked_expectations(self):
        scenarios = build_scenarios(self.server.base_url)

        self.assertEqual(len(scenarios), 3)
        self.assertEqual(
            {scenario.case.expected_result for scenario in scenarios},
            {FindingResult.PASSED, FindingResult.FAILED, FindingResult.INCONCLUSIVE},
        )

    def test_scenario_goals_do_not_disclose_the_expected_evaluation_result(self):
        scenarios = build_scenarios(self.server.base_url)

        for scenario in scenarios:
            with self.subTest(name=scenario.case.name):
                self.assertNotIn(scenario.case.expected_result.value, scenario.case.goal.lower())
                self.assertIn("$20", scenario.case.goal)
                self.assertNotIn("test-password", scenario.case.goal)
                self.assertNotIn("test-password", scenario.start_url)

    def test_scenarios_reject_a_non_loopback_sample_address(self):
        with self.assertRaises(ValueError):
            build_scenarios("https://example.com")

    def test_driver_starts_resets_and_stops_the_controlled_server(self):
        report = self.run_evaluation()

        self.assertTrue(report.passed)
        self.assertTrue(self.server.started)
        self.assertEqual(self.server.reset_count, 3)
        self.assertTrue(self.server.stopped)

    def test_each_scenario_uses_a_persisted_trace_from_the_agent(self):
        report = self.run_evaluation()

        self.assertEqual(report.total, 3)
        self.assertEqual(report.passed_count, 3)
        self.assertEqual(len(self.store.list_executions(application="controlled-sample", environment="local")), 3)
        self.assertEqual(len(self.agent.calls), 3)

    def test_explicit_human_login_and_approval_callbacks_are_forwarded(self):
        self.run_evaluation()

        for _, _, manual_login, approve_action in self.agent.calls:
            self.assertIs(manual_login, self.manual_login)
            self.assertIs(approve_action, self.approve_action)

    def test_agent_failure_is_reported_and_does_not_skip_other_scenarios(self):
        self.agent.failure_on = "/cart/b"

        report = self.run_evaluation()

        self.assertEqual(report.total, 3)
        self.assertEqual(report.failed_count, 1)
        self.assertEqual(report.passed_count, 2)
        self.assertIn("provider unavailable", report.format().lower())
        self.assertTrue(self.server.stopped)

    def test_wrong_agent_result_fails_the_known_bug_and_blocked_scenarios(self):
        self.agent.result_override = FindingResult.PASSED

        report = self.run_evaluation()

        self.assertFalse(report.passed)
        self.assertEqual(report.passed_count, 1)
        self.assertEqual(report.failed_count, 2)

    def test_preflight_failure_does_not_start_the_sample_server(self):
        self.agent.validation_error = RuntimeError("provider credentials unavailable")

        with self.assertRaisesRegex(RuntimeError, "provider credentials unavailable"):
            self.run_evaluation()

        self.assertFalse(self.server.started)

    def test_missing_or_uninstrumented_run_cannot_pass_evaluation(self):
        class UninstrumentedAgent:
            def validate(inner_self):
                pass

            def run(inner_self, *, start_url, goal, manual_login, approve_action):
                return self.store.save_execution(
                    application="controlled-sample", environment="local",
                    execution=Execution(findings=(), goal_verified=False),
                )

        self.agent = UninstrumentedAgent()
        report = self.run_evaluation()

        self.assertFalse(report.passed)
        self.assertEqual(report.failed_count, 3)
        self.assertIn("instrument", report.format().lower())

    def test_server_is_stopped_if_scenario_setup_fails(self):
        def fail_reset():
            raise RuntimeError("sample setup failed")

        self.server.reset = fail_reset

        report = self.run_evaluation()

        self.assertFalse(report.passed)
        self.assertTrue(self.server.stopped)
        self.assertIn("sample setup failed", report.format().lower())

    def test_evaluation_does_not_expose_fixture_password_in_report(self):
        report = self.run_evaluation()

        self.assertNotIn("test-password", report.format())


if __name__ == "__main__":
    unittest.main()
