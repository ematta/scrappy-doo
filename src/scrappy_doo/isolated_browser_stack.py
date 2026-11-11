"""Describe and manage a candidate stack through an injected runtime.

Container specs are requirements, not evidence that a runtime enforces them.
The candidate Docker adapter does not yet supply the proxy listener, certificate
authority, or viewer needed for a complete isolated browser session.
"""

import json
import re
from pathlib import Path
from uuid import uuid4

from scrappy_doo.strict_policy import DestinationRule, StrictPolicy


_IMAGE = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}\Z")


class IsolatedBrowserStack:
    def __init__(self, *, runtime, browser_image, proxy_image, policy_path):
        self._runtime = runtime
        self._browser_image = browser_image
        self._proxy_image = proxy_image
        self._policy_path = policy_path
        self._resources = []
        self._prefix = "scrappy-proof-" + uuid4().hex
        self._started = False

    def _validate(self):
        if any(not isinstance(image, str) or not _IMAGE.fullmatch(image)
               for image in (self._browser_image, self._proxy_image)):
            raise ValueError("Browser and proxy images must be pinned by digest")
        if not self._policy_path:
            raise ValueError("Proxy policy is required")
        try:
            policy = json.loads(Path(self._policy_path).read_text(encoding="utf-8"))
            if not isinstance(policy, dict) or not isinstance(policy.get("rules"), list):
                raise ValueError("Invalid proxy policy")
            rules = tuple(DestinationRule(**rule) for rule in policy["rules"])
            StrictPolicy(rules=rules)
        except (TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid proxy policy") from exc

    def start(self):
        if self._started or self._resources:
            raise RuntimeError("Stack is already running or requires cleanup")
        self._validate()
        inner = self._prefix + "-internal"
        outer = self._prefix + "-egress"
        proxy_name = self._prefix + "-proxy"
        proxy_address = f"http://{proxy_name}:8080"
        try:
            inner = self._runtime.create_network(inner, internal=True)
            self._resources.append(("network", inner))
            outer = self._runtime.create_network(outer, internal=False)
            self._resources.append(("network", outer))
            proxy = {
                "role": "proxy", "name": proxy_name, "image": self._proxy_image,
                "networks": [inner, outer], "internal_proxy_address": proxy_address,
                "policy_path": str(self._policy_path), "management_on_browser_network": False,
                "host_ports_published": False, "traffic_content_logging": False,
                "verify_upstream_tls": True, "private_key_proxy_only": True,
            }
            proxy_id = self._runtime.start_container(proxy)
            self._resources.append(("container", proxy_id))
            browser = {
                "role": "browser", "name": self._prefix + "-browser", "image": self._browser_image,
                "networks": [inner], "proxy_address": proxy_address, "privileged": False,
                "host_network": False, "capabilities_added": (), "non_root": True,
                "chromium_sandbox": True, "docker_socket_mounted": False,
                "host_credentials_mounted": False, "host_ports_published": False,
                "trust_proxy_public_certificate": True, "proxy_private_key_mounted": False,
                "ignore_https_errors": False,
            }
            browser_id = self._runtime.start_container(browser)
            self._resources.append(("container", browser_id))
            self._started = True
        except BaseException:
            self.stop()
            raise

    def stop(self):
        errors = []
        remaining = []
        for kind, name in reversed(self._resources):
            try:
                if kind == "container":
                    self._runtime.remove_container(name)
                else:
                    self._runtime.remove_network(name)
            except Exception as exc:
                errors.append(exc)
                remaining.append((kind, name))
        self._resources = list(reversed(remaining))
        self._started = False
        if errors:
            raise RuntimeError("Unable to remove all proof resources") from errors[0]
