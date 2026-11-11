"""Sandbox startup contract using Docker process doubles, not a live browser.

The real failure still needs a separate Chromium launch on the actual runtime.
These unit tests ensure a successful container start is not mistaken for a
working Chromium sandbox and that a failed check cannot weaken isolation.
"""

import unittest

from scrappy_doo.browser import BrowserConfig, BrowserError, BrowserSession
from scrappy_doo.docker_runtime import DockerRuntime


BROWSER_IMAGE = "browser@sha256:" + "a" * 64


def browser_spec():
    return {
        "role": "browser", "name": "proof-browser", "image": BROWSER_IMAGE,
        "networks": ["proof-internal"], "proxy_address": "http://proof-proxy:8080",
        "privileged": False, "host_network": False, "capabilities_added": (),
        "non_root": True, "chromium_sandbox": True, "docker_socket_mounted": False,
        "host_credentials_mounted": False, "host_ports_published": False,
        "trust_proxy_public_certificate": True, "proxy_private_key_mounted": False,
        "ignore_https_errors": False,
    }


class ProcessDouble:
    def __init__(self):
        self.calls = []
        self.probe_output = "SANDBOXED_BROWSER_STARTED\n"
        self.probe_error = None
        self.container_running = True

    def run(self, argv, *, timeout):
        self.calls.append(tuple(argv))
        if argv[1:2] == ("run",):
            if self.probe_error:
                raise self.probe_error
            return self.probe_output
        if argv[1:3] == ("container", "inspect"):
            return "true\n" if self.container_running else "false\n"
        return "resource-id\n"


def commands(process, *words):
    return [call for call in process.calls if call[1:1 + len(words)] == words]


class ChromiumSandboxStartupTests(unittest.TestCase):
    def setUp(self):
        self.process = ProcessDouble()
        self.runtime = DockerRuntime(process=self.process)

    def test_browser_is_not_started_until_a_sandboxed_launch_probe_succeeds(self):
        self.runtime.start_container(browser_spec())
        probe = commands(self.process, "run")
        create = commands(self.process, "container", "create")
        self.assertEqual(len(probe), 1)
        self.assertEqual(len(create), 1)
        self.assertLess(self.process.calls.index(probe[0]), self.process.calls.index(create[0]))
        self.assertIn(BROWSER_IMAGE, probe[0])

    def test_probe_is_temporary_network_disabled_nonroot_and_keeps_security_restrictions(self):
        self.runtime.start_container(browser_spec())
        probes = commands(self.process, "run")
        self.assertEqual(len(probes), 1, "A sandbox probe must run before browser startup")
        probe = probes[0]
        joined = " ".join(probe)
        self.assertIn("--rm", probe)
        self.assertTrue("--network=none" in probe or ("--network" in probe and "none" in probe))
        self.assertTrue(any(value in probe for value in ("--user=1000:1000", "1000:1000")))
        self.assertTrue("--cap-drop=ALL" in probe or ("--cap-drop" in probe and "ALL" in probe))
        self.assertIn("no-new-privileges", joined)
        self.assertNotIn("--privileged", probe)
        self.assertNotIn("--no-sandbox", joined)
        self.assertNotIn("seccomp=unconfined", joined)
        self.assertNotIn("--network=host", probe)
        self.assertNotIn("/var/run/docker.sock", joined)
        self.assertNotIn("--publish", joined)

    def test_probe_checks_actual_chromium_sandbox_not_just_container_start(self):
        self.runtime.start_container(browser_spec())
        probes = commands(self.process, "run")
        self.assertEqual(len(probes), 1, "A sandbox probe must run before browser startup")
        probe = probes[0]
        command_text = " ".join(probe)
        self.assertIn("chromiumSandbox", command_text)
        self.assertIn("SANDBOXED_BROWSER_STARTED", command_text)

    def test_no_usable_sandbox_stops_before_creating_browser_container(self):
        self.process.probe_error = RuntimeError("Chromium sandboxing failed: No usable sandbox")
        with self.assertRaisesRegex(RuntimeError, "(?i)sandbox"):
            self.runtime.start_container(browser_spec())
        self.assertEqual(commands(self.process, "container", "create"), [])
        self.assertEqual(commands(self.process, "container", "start"), [])

    def test_runtime_failure_does_not_retry_without_the_sandbox_or_start_browser(self):
        self.process.probe_error = RuntimeError("OCI runtime start failed")
        with self.assertRaises(RuntimeError):
            self.runtime.start_container(browser_spec())
        self.assertEqual(len(commands(self.process, "run")), 1)
        self.assertEqual(commands(self.process, "container", "create"), [])
        self.assertNotIn("--no-sandbox", " ".join(map(" ".join, self.process.calls)))

    def test_missing_startup_marker_is_not_a_successful_sandbox_check(self):
        self.process.probe_output = "container started\n"
        with self.assertRaisesRegex(RuntimeError, "(?i)sandbox"):
            self.runtime.start_container(browser_spec())
        self.assertEqual(commands(self.process, "container", "create"), [])

    def test_browser_exiting_during_startup_is_reported_and_cleaned_up(self):
        self.process.container_running = False
        with self.assertRaises(RuntimeError):
            self.runtime.start_container(browser_spec())
        self.assertTrue(commands(self.process, "container", "inspect"))
        self.assertTrue(commands(self.process, "container", "rm"))
        self.assertIn("proof-browser", commands(self.process, "container", "rm")[0])

    def test_sandbox_probe_success_does_not_unlock_strict_mode(self):
        self.runtime.start_container(browser_spec())
        class Transport:
            def start(self, command):
                raise AssertionError("Strict mode cannot start from a sandbox probe")
        session = BrowserSession(
            config=BrowserConfig(allowed_origins=("https://app.example",), boundary_mode="strict"),
            transport=Transport(),
        )
        with self.assertRaises(BrowserError):
            session.start()


if __name__ == "__main__":
    unittest.main()
