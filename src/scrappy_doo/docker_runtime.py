"""Docker command adapter for candidate proof containers.

Container settings alone do not demonstrate enforcement. A compatible proxy,
certificate provisioning, and a protected viewer are still required before a
live browser can use this adapter for an isolation proof.
"""

import hashlib
import re
import subprocess
from pathlib import Path


_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z")
_IMAGE = re.compile(r"[^\s@]+@sha256:[a-f0-9]{64}\Z")
_SECCOMP_PATH = Path(__file__).with_name("playwright_seccomp_profile.json")
_SECCOMP_SHA256 = "611eebf598ca7356445d270165ca4e5f17b77d5a6ac7ede56aca49120d72121a"
_SANDBOX_MARKER = "SANDBOXED_BROWSER_STARTED"
_SANDBOX_SCRIPT = (
    'const {chromium}=require("/opt/scrappy/node_modules/playwright");'
    '(async()=>{const browser=await chromium.launch('
    '{headless:false,chromiumSandbox:true,timeout:10000});'
    'console.log("SANDBOXED_BROWSER_STARTED");await browser.close()})()'
    '.catch(()=>{console.error("Sandboxed Chromium startup failed");process.exitCode=1})'
)


class DockerProcess:
    def run(self, argv, *, timeout):
        try:
            completed = subprocess.run(argv, check=True, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("Docker operation failed") from exc
        return completed.stdout


class DockerRuntime:
    def __init__(self, *, process=None, timeout=60):
        self._process = process if process is not None else DockerProcess()
        self._timeout = timeout

    def _run(self, *args):
        return self._process.run(("docker", *args), timeout=self._timeout)

    @staticmethod
    def _name(name):
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError("Invalid Docker resource name")
        return name

    def create_network(self, name, *, internal):
        name = self._name(name)
        if not isinstance(internal, bool):
            raise ValueError("Network isolation must be explicit")
        self._run("network", "create", *(('--internal',) if internal else ()), name)
        return name

    def _verify_browser_sandbox(self, image, seccomp_option):
        """Require a real sandboxed launch under the browser's restrictions."""
        try:
            output = self._run(
                "run", "--rm", "--network=none", "--user=1000:1000",
                "--cap-drop=ALL", "--security-opt=no-new-privileges", seccomp_option, "--init",
                "--entrypoint", "timeout", image, "20", "xvfb-run", "-a",
                "node", "-e", _SANDBOX_SCRIPT,
            )
        except RuntimeError as exc:
            raise RuntimeError("Sandboxed Chromium startup check failed") from exc
        if _SANDBOX_MARKER not in output.splitlines():
            raise RuntimeError("Sandboxed Chromium startup check returned no success marker")

    @staticmethod
    def _browser_seccomp_option():
        try:
            digest = hashlib.sha256(_SECCOMP_PATH.read_bytes()).hexdigest()
        except OSError as exc:
            raise RuntimeError("Browser seccomp profile is unavailable") from exc
        if digest != _SECCOMP_SHA256:
            raise RuntimeError("Browser seccomp profile does not match the pinned profile")
        return f"--security-opt=seccomp={_SECCOMP_PATH}"

    def start_container(self, spec):
        if not isinstance(spec, dict):
            raise ValueError("Invalid container specification")
        name = self._name(spec.get("name"))
        image = spec.get("image")
        role = spec.get("role")
        networks = spec.get("networks")
        if not isinstance(image, str) or not _IMAGE.fullmatch(image):
            raise ValueError("Container image must be pinned by digest")
        if not isinstance(networks, list) or len(set(networks)) != len(networks):
            raise ValueError("Invalid container networks")
        for network in networks:
            self._name(network)
            if network in {"host", "bridge", "none"}:
                raise ValueError("A dedicated proof network is required")
        options = []
        if role == "browser":
            required_false = ("privileged", "host_network", "docker_socket_mounted", "host_credentials_mounted", "host_ports_published", "proxy_private_key_mounted", "ignore_https_errors")
            required_true = ("non_root", "chromium_sandbox", "trust_proxy_public_certificate")
            if (len(networks) != 1 or spec.get("capabilities_added") != ()
                    or any(spec.get(field) is not False for field in required_false)
                    or any(spec.get(field) is not True for field in required_true)):
                raise ValueError("Unsafe browser specification")
            proxy = spec.get("proxy_address")
            if not isinstance(proxy, str) or not re.fullmatch(r"http://[a-zA-Z0-9][a-zA-Z0-9_.-]*:8080", proxy):
                raise ValueError("Invalid proxy address")
            options = ["--interactive", "--init", "--user=1000:1000",
                       "--env", "SCRAPPY_PROXY_ADDRESS=" + proxy]
        elif role == "proxy":
            required_false = ("management_on_browser_network", "host_ports_published", "traffic_content_logging")
            required_true = ("verify_upstream_tls", "private_key_proxy_only")
            if (len(networks) != 2 or any(spec.get(field) is not False for field in required_false)
                    or any(spec.get(field) is not True for field in required_true)):
                raise ValueError("Unsafe proxy specification")
            path = spec.get("policy_path")
            if not isinstance(path, str) or not path.startswith("/") or "," in path:
                raise ValueError("Invalid policy path")
            options = ["--mount", f"type=bind,src={path},dst=/etc/scrappy/policy.json,readonly"]
        else:
            raise ValueError("Unknown container role")
        if role == "browser":
            seccomp_option = self._browser_seccomp_option()
            self._verify_browser_sandbox(image, seccomp_option)
            options.append(seccomp_option)
        self._run("container", "create", "--name", name, "--network", networks[0],
                  "--cap-drop=ALL", "--security-opt=no-new-privileges", *options, image)
        try:
            for network in networks[1:]:
                self._run("network", "connect", network, name)
            self._run("container", "start", name)
            if role == "browser":
                running = self._run("container", "inspect", "--format", "{{.State.Running}}", name)
                if running.strip() != "true":
                    raise RuntimeError("Browser container exited during startup")
        except BaseException:
            try:
                self.remove_container(name)
            except Exception as cleanup_error:
                raise RuntimeError("Container startup and cleanup failed") from cleanup_error
            raise
        return name

    def remove_container(self, name):
        self._run("container", "rm", "--force", self._name(name))

    def remove_network(self, name):
        self._run("network", "rm", self._name(name))
