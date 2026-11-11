"""Container-runtime adapter contract; all Docker invocations are doubles."""

import unittest


class DockerProcessDouble:
    def __init__(self):
        self.calls = []
        self.fail_contains = None

    def run(self, argv, *, timeout):
        self.calls.append(tuple(argv))
        if self.fail_contains and self.fail_contains in argv:
            raise RuntimeError("Docker operation failed")
        if argv[1:2] == ("run",):
            return "SANDBOXED_BROWSER_STARTED\n"
        if argv[1:3] == ("container", "inspect"):
            return "true\n"
        return "resource-id\n"


def runtime(process):
    from scrappy_doo.docker_runtime import DockerRuntime
    return DockerRuntime(process=process)


def browser_spec(**changes):
    spec = {
        "role": "browser", "name": "proof-browser", "image": "browser@sha256:" + "a" * 64,
        "networks": ["proof-internal"], "proxy_address": "http://proof-proxy:8080",
        "privileged": False, "host_network": False, "capabilities_added": (), "non_root": True,
        "chromium_sandbox": True, "docker_socket_mounted": False, "host_credentials_mounted": False,
        "host_ports_published": False, "trust_proxy_public_certificate": True,
        "proxy_private_key_mounted": False, "ignore_https_errors": False,
    }
    spec.update(changes)
    return spec


def proxy_spec(**changes):
    spec = {
        "role": "proxy", "name": "proof-proxy", "image": "proxy@sha256:" + "b" * 64,
        "networks": ["proof-internal", "proof-egress"], "internal_proxy_address": "http://proof-proxy:8080",
        "policy_path": "/fixture/policy.json", "management_on_browser_network": False,
        "host_ports_published": False, "traffic_content_logging": False,
        "verify_upstream_tls": True, "private_key_proxy_only": True,
    }
    spec.update(changes)
    return spec


class DockerRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.process = DockerProcessDouble()

    def test_internal_network_creation_sets_internal_flag(self):
        runtime(self.process).create_network("proof-internal", internal=True)
        command = self.process.calls[0]
        self.assertEqual(command[:3], ("docker", "network", "create"))
        self.assertIn("--internal", command)
        self.assertIn("proof-internal", command)

    def test_browser_container_is_nonroot_and_only_joins_internal_network(self):
        runtime(self.process).start_container(browser_spec())
        commands = self.process.calls
        create = next(call for call in commands if "create" in call and "container" in call)
        self.assertIn("proof-internal", create)
        self.assertIn("browser@sha256:" + "a" * 64, create)
        self.assertIn("--cap-drop=ALL", create)
        self.assertIn("--security-opt=no-new-privileges", create)
        self.assertNotIn("--privileged", create)
        self.assertNotIn("--network=host", create)
        self.assertFalse(any("/var/run/docker.sock" in arg for arg in create))
        self.assertFalse(any("--publish" in arg or arg == "-p" for arg in create))
        self.assertFalse(any(call[1:3] == ("network", "connect") for call in commands))

    def test_proxy_joins_egress_network_before_it_starts(self):
        runtime(self.process).start_container(proxy_spec())
        calls = self.process.calls
        connect = next(i for i, call in enumerate(calls) if call[1:3] == ("network", "connect"))
        start = next(i for i, call in enumerate(calls) if call[1:3] == ("container", "start"))
        self.assertLess(connect, start)
        self.assertIn("proof-egress", calls[connect])

    def test_unsafe_browser_spec_is_rejected_without_invoking_docker(self):
        for change in ({"privileged": True}, {"host_network": True},
                       {"docker_socket_mounted": True}, {"ignore_https_errors": True},
                       {"networks": ["proof-internal", "other"]}, {"non_root": False}):
            with self.subTest(change=change):
                process = DockerProcessDouble()
                with self.assertRaises(ValueError):
                    runtime(process).start_container(browser_spec(**change))
                self.assertEqual(process.calls, [])

    def test_proxy_management_and_unverified_tls_cannot_be_enabled(self):
        for change in ({"management_on_browser_network": True}, {"verify_upstream_tls": False},
                       {"traffic_content_logging": True}, {"host_ports_published": True}):
            with self.subTest(change=change):
                process = DockerProcessDouble()
                with self.assertRaises(ValueError):
                    runtime(process).start_container(proxy_spec(**change))
                self.assertEqual(process.calls, [])

    def test_failed_network_attach_cleans_up_the_new_container(self):
        self.process.fail_contains = "connect"
        with self.assertRaises(RuntimeError):
            runtime(self.process).start_container(proxy_spec())
        self.assertTrue(any(call[1:3] == ("container", "rm") and "proof-proxy" in call for call in self.process.calls))
        self.assertFalse(any(call[1:3] == ("container", "start") for call in self.process.calls))

    def test_resource_names_are_not_interpreted_by_a_shell(self):
        with self.assertRaises(ValueError):
            runtime(self.process).create_network("proof; touch /tmp/unsafe", internal=True)
        self.assertEqual(self.process.calls, [])

    def test_removal_targets_only_the_requested_resource(self):
        adapter = runtime(self.process)
        adapter.remove_container("proof-browser")
        adapter.remove_network("proof-internal")
        self.assertEqual(self.process.calls[0][1:3], ("container", "rm"))
        self.assertIn("proof-browser", self.process.calls[0])
        self.assertEqual(self.process.calls[1][1:3], ("network", "rm"))
        self.assertIn("proof-internal", self.process.calls[1])


if __name__ == "__main__":
    unittest.main()
