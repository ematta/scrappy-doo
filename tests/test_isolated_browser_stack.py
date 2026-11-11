"""Unit contract for the candidate isolated browser/proxy stack.

The runtime double records what would be submitted to a container runtime.
These tests do not start containers or demonstrate network isolation.
"""

import tempfile
import unittest
from pathlib import Path

from scrappy_doo.browser import BrowserConfig, BrowserError, BrowserSession


BROWSER_IMAGE = "browser@sha256:" + "a" * 64
PROXY_IMAGE = "proxy@sha256:" + "b" * 64


class RuntimeDouble:
    def __init__(self):
        self.networks = []
        self.containers = []
        self.removed = []
        self.fail_on = None

    def create_network(self, name, *, internal):
        if self.fail_on == "network":
            raise RuntimeError("network unavailable")
        self.networks.append((name, internal))
        return name

    def start_container(self, spec):
        if self.fail_on == spec["role"]:
            raise RuntimeError("container startup failed")
        self.containers.append(spec)
        return spec["name"]

    def remove_container(self, name):
        self.removed.append(("container", name))

    def remove_network(self, name):
        self.removed.append(("network", name))


def make_stack(runtime, **overrides):
    from scrappy_doo.isolated_browser_stack import IsolatedBrowserStack

    options = {
        "runtime": runtime,
        "browser_image": BROWSER_IMAGE,
        "proxy_image": PROXY_IMAGE,
        "policy_path": runtime.policy_path,
    }
    options.update(overrides)
    return IsolatedBrowserStack(**options)


class IsolatedBrowserStackTests(unittest.TestCase):
    def setUp(self):
        self.runtime = RuntimeDouble()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        policy = Path(directory.name) / "policy.json"
        policy.write_text('{"rules":[{"scheme":"https","host":"app.example","port":443}]}')
        self.runtime.policy_path = str(policy)

    def test_browser_uses_only_a_new_internal_network_and_proxy(self):
        stack = make_stack(self.runtime)
        stack.start()
        internal = [name for name, is_internal in self.runtime.networks if is_internal]
        self.assertEqual(len(internal), 1)
        browser = next(spec for spec in self.runtime.containers if spec["role"] == "browser")
        proxy = next(spec for spec in self.runtime.containers if spec["role"] == "proxy")
        self.assertEqual(browser["networks"], internal)
        self.assertIn(internal[0], proxy["networks"])
        self.assertEqual(len(proxy["networks"]), 2)
        self.assertEqual(browser["proxy_address"], proxy["internal_proxy_address"])

    def test_browser_has_no_host_network_privileges_or_host_credentials(self):
        stack = make_stack(self.runtime)
        stack.start()
        browser = next(spec for spec in self.runtime.containers if spec["role"] == "browser")
        self.assertFalse(browser["privileged"])
        self.assertFalse(browser["host_network"])
        self.assertEqual(browser["capabilities_added"], ())
        self.assertTrue(browser["non_root"])
        self.assertTrue(browser["chromium_sandbox"])
        self.assertFalse(browser["docker_socket_mounted"])
        self.assertFalse(browser["host_credentials_mounted"])
        self.assertFalse(browser["host_ports_published"])

    def test_proxy_management_is_not_exposed_to_browser_network(self):
        stack = make_stack(self.runtime)
        stack.start()
        proxy = next(spec for spec in self.runtime.containers if spec["role"] == "proxy")
        self.assertFalse(proxy["management_on_browser_network"])
        self.assertFalse(proxy["host_ports_published"])
        self.assertFalse(proxy["traffic_content_logging"])
        self.assertTrue(proxy["verify_upstream_tls"])

    def test_public_interception_certificate_only_is_available_to_browser(self):
        stack = make_stack(self.runtime)
        stack.start()
        browser = next(spec for spec in self.runtime.containers if spec["role"] == "browser")
        proxy = next(spec for spec in self.runtime.containers if spec["role"] == "proxy")
        self.assertTrue(browser["trust_proxy_public_certificate"])
        self.assertFalse(browser["proxy_private_key_mounted"])
        self.assertTrue(proxy["private_key_proxy_only"])
        self.assertFalse(browser["ignore_https_errors"])

    def test_unpinned_images_are_rejected_before_creating_resources(self):
        for field in ("browser_image", "proxy_image"):
            with self.subTest(field=field):
                runtime = RuntimeDouble()
                runtime.policy_path = self.runtime.policy_path
                with self.assertRaises(ValueError):
                    make_stack(runtime, **{field: "example:latest"}).start()
                self.assertEqual(runtime.networks, [])
                self.assertEqual(runtime.containers, [])

    def test_missing_policy_fails_closed_before_browser_starts(self):
        with self.assertRaises((ValueError, FileNotFoundError)):
            make_stack(self.runtime, policy_path=None).start()
        self.assertFalse(any(spec["role"] == "browser" for spec in self.runtime.containers))

    def test_proxy_failure_prevents_browser_start_and_cleans_created_resources(self):
        self.runtime.fail_on = "proxy"
        with self.assertRaises(RuntimeError):
            make_stack(self.runtime).start()
        self.assertFalse(any(spec["role"] == "browser" for spec in self.runtime.containers))
        for name, _ in self.runtime.networks:
            self.assertIn(("network", name), self.runtime.removed)

    def test_browser_start_failure_cleans_proxy_and_networks(self):
        self.runtime.fail_on = "browser"
        with self.assertRaises(RuntimeError):
            make_stack(self.runtime).start()
        for spec in self.runtime.containers:
            self.assertIn(("container", spec["name"]), self.runtime.removed)
        for name, _ in self.runtime.networks:
            self.assertIn(("network", name), self.runtime.removed)

    def test_stop_cleans_only_resources_created_for_this_stack(self):
        stack = make_stack(self.runtime)
        stack.start()
        stack.stop()
        self.assertEqual(
            set(self.runtime.removed),
            {("network", name) for name, _ in self.runtime.networks}
            | {("container", spec["name"]) for spec in self.runtime.containers},
        )

    def test_stack_creation_does_not_unlock_strict_browser_mode(self):
        stack = make_stack(self.runtime)
        stack.start()
        class Transport:
            def start(self, command):
                raise AssertionError("strict browser started without live proof")
        browser = BrowserSession(
            config=BrowserConfig(allowed_origins=("https://app.example",), boundary_mode="strict"),
            transport=Transport(),
        )
        with self.assertRaises(BrowserError):
            browser.start()


if __name__ == "__main__":
    unittest.main()
