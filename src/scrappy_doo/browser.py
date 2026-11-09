"""Playwright MCP browser sessions and a local stdio transport."""

import json
import os
import select
import subprocess
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from scrappy_doo.orchestration import Tool


class BrowserError(Exception):
    """A browser action is unavailable or failed."""


class StdioMCPTransport:
    """Minimal synchronous MCP client for a local subprocess."""

    def __init__(self, *, timeout: float = 60):
        self._process: subprocess.Popen[bytes] | None = None
        self._buffer = b""
        self._next_id = 0
        self._timeout = timeout

    def _send(self, message: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise BrowserError("MCP process is not running")
        try:
            self._process.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
            self._process.stdin.flush()
        except (OSError, BrokenPipeError) as error:
            raise BrowserError("MCP process disconnected") from error

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._process is None or self._process.stdout is None:
            raise BrowserError("MCP process is not running")
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self._timeout
        while True:
            if b"\n" not in self._buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([self._process.stdout], [], [], max(0, remaining))[0]:
                    raise BrowserError("MCP request timed out")
                chunk = os.read(self._process.stdout.fileno(), 65536)
                if not chunk:
                    raise BrowserError("MCP process disconnected")
                self._buffer += chunk
                if len(self._buffer) > 10_000_000:
                    raise BrowserError("MCP response exceeds the size limit")
                continue
            line, self._buffer = self._buffer.split(b"\n", 1)
            try:
                message = json.loads(line)
            except (ValueError, UnicodeError):
                raise BrowserError("Invalid MCP response") from None
            if not isinstance(message, dict):
                raise BrowserError("Invalid MCP response")
            if "method" in message and "id" in message:
                reply = {"jsonrpc": "2.0", "id": message["id"]}
                if message["method"] == "ping":
                    reply["result"] = {}
                else:
                    reply["error"] = {"code": -32601, "message": "Method not supported"}
                self._send(reply)
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message or not isinstance(message.get("result"), dict):
                raise BrowserError("MCP request failed")
            return message["result"]

    def start(self, command: tuple[str, ...]) -> None:
        if self._process is not None:
            raise BrowserError("MCP process already started")
        environment = {key: value for key, value in os.environ.items() if not key.startswith("PLAYWRIGHT_MCP_")}
        try:
            self._process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=environment, bufsize=0,
            )
            result = self._request("initialize", {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "scrappy-doo", "version": "0.1.0"},
            })
            if result.get("protocolVersion") not in {"2025-06-18", "2025-03-26", "2024-11-05"}:
                raise BrowserError("Unsupported MCP protocol version")
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:
            self.stop()
            raise

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise BrowserError("Playwright MCP tool failed")
        content = result.get("content")
        if not isinstance(content, list):
            raise BrowserError("Invalid MCP tool response")
        texts = [item["text"] for item in content if isinstance(item, dict)
                 and item.get("type") == "text" and isinstance(item.get("text"), str)]
        if not texts:
            raise BrowserError("MCP tool returned no text observation")
        return "\n".join(texts)

    def stop(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        if process.stdin:
            process.stdin.close()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if process.stdout:
            process.stdout.close()
        self._buffer = b""


@dataclass(frozen=True)
class BrowserConfig:
    allowed_origins: tuple[str, ...]
    boundary_mode: str
    capture_screenshots: bool = False
    command: tuple[str, ...] = ("npx", "--no-install", "@playwright/mcp")


def _origin(url: str, *, require_origin: bool = False) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or (require_origin and (parsed.path not in ("", "/") or parsed.query or parsed.fragment))):
            raise ValueError
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return parsed.scheme, parsed.hostname.encode("idna").decode("ascii").lower(), port
    except (ValueError, TypeError, UnicodeError):
        raise BrowserError("Invalid browser origin or URL") from None


class BrowserSession:
    _TOOLS = {
        "browser_navigate": ("url",),
        "browser_snapshot": (),
        "browser_click": ("target",),
        "browser_type": ("target", "text"),
        "browser_press_key": ("key",),
        "browser_take_screenshot": (),
    }

    def __init__(self, *, config: BrowserConfig, transport: Any | None = None):
        self.config = config
        self._transport = transport if transport is not None else StdioMCPTransport()
        self._started = False
        self._manual_sign_in = False
        self.warnings: tuple[str, ...] = ()

    def start(self) -> None:
        if self._started:
            raise BrowserError("Browser session already started")
        if self.config.boundary_mode != "best_effort":
            raise BrowserError("Strict isolation is not available")
        if not self.config.allowed_origins or any(";" in origin for origin in self.config.allowed_origins):
            raise BrowserError("Explicit allowed origins are required")
        for origin in self.config.allowed_origins:
            _origin(origin, require_origin=True)
        command = (*self.config.command, "--isolated", "--allowed-origins=" + ";".join(self.config.allowed_origins))
        self._transport.start(command)
        self._started = True
        self.warnings = ("Best-effort origin filtering is not a security boundary and does not control redirects.",)

    def begin_manual_sign_in(self) -> None:
        if not self._started or self._manual_sign_in:
            raise BrowserError("Cannot begin manual sign-in")
        self._manual_sign_in = True

    def finish_manual_sign_in(self) -> None:
        if not self._started or not self._manual_sign_in:
            raise BrowserError("Manual sign-in is not active")
        self._manual_sign_in = False

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        if not self._started or self._manual_sign_in:
            raise BrowserError("Browser actions are unavailable")
        if name not in self._TOOLS or not isinstance(arguments, dict):
            raise BrowserError("Browser tool is not allowed")
        if name == "browser_take_screenshot" and not self.config.capture_screenshots:
            raise BrowserError("Screenshot capture is disabled")
        if set(arguments) != set(self._TOOLS[name]) or not all(isinstance(value, str) for value in arguments.values()):
            raise BrowserError("Invalid browser tool arguments")
        if name == "browser_navigate":
            allowed = {_origin(origin, require_origin=True) for origin in self.config.allowed_origins}
            if _origin(arguments["url"]) not in allowed:
                raise BrowserError("Navigation origin is not configured")
        return self._transport.call_tool(name, arguments)

    def as_tools(self) -> dict[str, Tool]:
        names = {name: arguments for name, arguments in self._TOOLS.items()
                 if name != "browser_take_screenshot" or self.config.capture_screenshots}
        return {
            name: Tool(handler=lambda _name=name, **arguments: self.call(_name, arguments),
                       required_arguments=required)
            for name, required in names.items()
        }

    def close(self) -> None:
        if self._started:
            self._started = False
            self._manual_sign_in = False
            self._transport.stop()
