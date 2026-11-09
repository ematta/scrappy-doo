"""Proposed Playwright MCP adapter contract for coverage review."""

import unittest

from scrappy_doo.browser import BrowserConfig, BrowserError, BrowserSession
from scrappy_doo.execution import FindingResult
from scrappy_doo.orchestration import ExecutionRunner, RunPolicy, ToolCall


class FakeMCP:
    """MCP boundary double; it never launches a process or browser."""

    def __init__(self):
        self.commands = []
        self.calls = []
        self.stopped = False
        self.failure = None

    def start(self, command):
        self.commands.append(tuple(command))

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.failure:
            raise self.failure
        return "Cart total: $20"

    def stop(self):
        self.stopped = True


class OneCallModel:
    def respond(self, *, goal, observations):
        return ToolCall(name="browser_snapshot", arguments={})


class BrowserSessionTests(unittest.TestCase):
    def setUp(self):
        self.mcp = FakeMCP()
        self.config = BrowserConfig(
            allowed_origins=("https://shop.example", "https://login.example"),
            boundary_mode="best_effort",
            capture_screenshots=False,
        )
        self.browser = BrowserSession(config=self.config, transport=self.mcp)

    def test_start_uses_isolated_visible_browser_with_configured_origins(self):
        self.browser.start()

        command = self.mcp.commands[0]
        self.assertIn("--isolated", command)
        self.assertNotIn("--headless", command)
        self.assertIn("--allowed-origins=https://shop.example;https://login.example", command)
        self.assertFalse(any("allow-unrestricted-file-access" in argument for argument in command))

    def test_best_effort_mode_discloses_redirect_and_security_boundary_limitations(self):
        self.browser.start()

        warning = " ".join(self.browser.warnings).lower()
        self.assertIn("redirect", warning)
        self.assertIn("security boundary", warning)

    def test_strict_mode_is_unavailable_without_verified_enforcement(self):
        browser = BrowserSession(
            config=BrowserConfig(allowed_origins=("https://shop.example",), boundary_mode="strict"),
            transport=self.mcp,
        )

        with self.assertRaises(BrowserError):
            browser.start()
        self.assertEqual(self.mcp.commands, [])

    def test_navigate_and_snapshot_are_forwarded_only_as_allowed_tools(self):
        self.browser.start()

        self.browser.call("browser_navigate", {"url": "https://shop.example/cart"})
        result = self.browser.call("browser_snapshot", {})

        self.assertEqual(self.mcp.calls, [
            ("browser_navigate", {"url": "https://shop.example/cart"}),
            ("browser_snapshot", {}),
        ])
        self.assertEqual(result, "Cart total: $20")

    def test_direct_navigation_to_an_unconfigured_origin_is_rejected(self):
        self.browser.start()

        with self.assertRaises(BrowserError):
            self.browser.call("browser_navigate", {"url": "https://other.example/"})
        self.assertEqual(self.mcp.calls, [])

    def test_arbitrary_code_and_unknown_mcp_tools_are_not_exposed(self):
        self.browser.start()

        for name in ("browser_run_code", "browser_evaluate", "unknown"):
            with self.subTest(name=name), self.assertRaises(BrowserError):
                self.browser.call(name, {})
        self.assertEqual(self.mcp.calls, [])

    def test_screenshot_call_is_blocked_when_capture_is_disabled(self):
        self.browser.start()

        with self.assertRaises(BrowserError):
            self.browser.call("browser_take_screenshot", {})
        self.assertEqual(self.mcp.calls, [])

    def test_manual_sign_in_suspends_observation_and_actions_until_finished(self):
        self.browser.start()
        self.browser.begin_manual_sign_in()

        for name, arguments in (("browser_snapshot", {}), ("browser_click", {"ref": "button"})):
            with self.subTest(name=name), self.assertRaises(BrowserError):
                self.browser.call(name, arguments)
        self.assertEqual(self.mcp.calls, [])

        self.browser.finish_manual_sign_in()
        self.assertEqual(self.browser.call("browser_snapshot", {}), "Cart total: $20")

    def test_screenshot_is_also_blocked_during_manual_sign_in_when_capture_is_enabled(self):
        browser = BrowserSession(
            config=BrowserConfig(
                allowed_origins=("https://shop.example",), boundary_mode="best_effort",
                capture_screenshots=True,
            ),
            transport=self.mcp,
        )
        browser.start()
        browser.begin_manual_sign_in()

        with self.assertRaises(BrowserError):
            browser.call("browser_take_screenshot", {})
        self.assertEqual(self.mcp.calls, [])

    def test_closing_session_stops_mcp_and_prevents_further_calls(self):
        self.browser.start()
        self.browser.close()

        self.assertTrue(self.mcp.stopped)
        with self.assertRaises(BrowserError):
            self.browser.call("browser_snapshot", {})
        self.assertEqual(self.mcp.calls, [])

    def test_mcp_failure_is_a_blocker_in_the_execution_runner(self):
        self.browser.start()
        self.mcp.failure = OSError("MCP disconnected")
        runner = ExecutionRunner(model=OneCallModel(), tools=self.browser.as_tools())

        execution = runner.run(
            goal="Inspect the cart",
            policy=RunPolicy(allowed_tools=frozenset({"browser_snapshot"}), max_tool_calls=1),
        )

        self.assertEqual(execution.overall_result, FindingResult.INCONCLUSIVE)
        self.assertTrue(any("MCP disconnected" in (finding.reason or "") for finding in execution.findings))


if __name__ == "__main__":
    unittest.main()
