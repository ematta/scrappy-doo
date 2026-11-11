"""Static checks for the proposed browser build context, not image execution."""

import json
import re
import unittest
from pathlib import Path


CONTEXT = Path(__file__).resolve().parents[1] / "containers" / "browser"


def artifact(test, name):
    path = CONTEXT / name
    test.assertTrue(path.is_file(), f"Missing browser build artifact: {path}")
    return path.read_text(encoding="utf-8")


def instructions(text):
    return "\n".join(line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#"))


class BrowserImageTests(unittest.TestCase):
    def test_dockerfile_uses_a_digest_pinned_base(self):
        dockerfile = instructions(artifact(self, "Dockerfile"))
        bases = re.findall(r"(?im)^FROM\s+(\S+)", dockerfile)
        self.assertTrue(bases)
        for base in bases:
            self.assertRegex(base, r"@sha256:[a-f0-9]{64}$")

    def test_dockerfile_finishes_as_a_nonroot_user(self):
        dockerfile = instructions(artifact(self, "Dockerfile"))
        users = re.findall(r"(?im)^USER\s+(\S+)", dockerfile)
        self.assertTrue(users)
        self.assertNotIn(users[-1], ("root", "0", "0:0"))

    def test_dockerfile_has_a_real_entrypoint_and_does_not_copy_ca_private_keys(self):
        dockerfile = instructions(artifact(self, "Dockerfile"))
        self.assertRegex(dockerfile, r"(?im)^ENTRYPOINT\b")
        for line in dockerfile.splitlines():
            if line.upper().startswith(("COPY ", "ADD ")):
                self.assertNotRegex(line.lower(), r"(?:ca[-_.]?key|private[-_.]?key|\.key\b)")

    def test_build_manifest_pins_browser_and_playwright_mcp_versions(self):
        manifest = json.loads(artifact(self, "manifest.json"))
        for key in ("browser_version", "playwright_mcp_version"):
            self.assertRegex(manifest[key], r"^\d+\.\d+(?:\.\d+){1,2}$")

    def test_browser_entrypoint_requires_public_ca_and_proxy(self):
        entrypoint = instructions(artifact(self, "entrypoint.sh"))
        self.assertIn("PROXY", entrypoint.upper())
        self.assertRegex(entrypoint.lower(), r"certutil|trust")
        self.assertNotIn("--ignore-https-errors", entrypoint)
        self.assertNotIn("--no-sandbox", entrypoint)

    def test_browser_entrypoint_runs_headed_playwright_mcp(self):
        entrypoint = instructions(artifact(self, "entrypoint.sh"))
        self.assertRegex(entrypoint.lower(), r"playwright|@playwright/mcp")
        self.assertNotIn("--headless", entrypoint)
        self.assertRegex(entrypoint.lower(), r"display|xvfb")


if __name__ == "__main__":
    unittest.main()
