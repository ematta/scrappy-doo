"""Small loopback-only application with known QA outcomes."""

import hmac
import json
import secrets
import threading
from dataclasses import dataclass
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


@dataclass(frozen=True)
class SampleResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class SampleApplication:
    """In-memory login and cart scenarios; credentials are public fixture data."""

    def __init__(self):
        self._sessions: set[str] = set()
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._sessions.clear()

    @staticmethod
    def _response(status: int, text: str, headers: dict[str, str] | None = None) -> SampleResponse:
        return SampleResponse(status, {"Content-Type": "text/html; charset=utf-8", **(headers or {})}, text.encode("utf-8"))

    def _authenticated(self, headers: dict[str, str]) -> bool:
        cookie = SimpleCookie()
        try:
            cookie.load(next((value for name, value in headers.items() if name.lower() == "cookie"), ""))
            token = cookie["sample_session"].value if "sample_session" in cookie else ""
        except Exception:
            return False
        with self._lock:
            return token in self._sessions

    def handle(self, *, method: str, path: str, headers: dict[str, str] | None = None, body: bytes = b"") -> SampleResponse:
        headers = headers or {}
        parsed = urlsplit(path)
        route = parsed.path
        if method == "GET" and route == "/login":
            next_path = parse_qs(parsed.query).get("next", ["/cart/a"])[0]
            if next_path not in ("/cart/a", "/cart/b", "/cart/c"):
                next_path = "/cart/a"
            return self._response(200, (
                "<!doctype html><html><head><title>Sample login</title></head><body>"
                "<h1>Login</h1><form method='post' action='/login'>"
                "<label>Username <input name='username' autocomplete='username'></label>"
                "<label>Password <input type='password' name='password' autocomplete='current-password'></label>"
                f"<input type='hidden' name='next' value='{next_path}'>"
                "<button type='submit'>Sign in</button></form></body></html>"
            ))
        if method == "POST" and route == "/login":
            try:
                content_type = next((value for name, value in headers.items() if name.lower() == "content-type"), "")
                if "application/x-www-form-urlencoded" in content_type:
                    form = parse_qs(body.decode("utf-8"))
                    data = {key: values[0] for key, values in form.items() if values}
                else:
                    data = json.loads(body.decode("utf-8"))
                if not isinstance(data, dict):
                    raise ValueError
                username, password = data.get("username"), data.get("password")
                if not isinstance(username, str) or not isinstance(password, str):
                    raise ValueError
            except (UnicodeError, ValueError, TypeError):
                return self._response(400, "Invalid login request")
            if not (hmac.compare_digest(username, "qa") and hmac.compare_digest(password, "test-password")):
                return self._response(401, "Invalid credentials")
            token = secrets.token_urlsafe(32)
            with self._lock:
                self._sessions.add(token)
            next_path = data.get("next", "/cart/a")
            if next_path not in ("/cart/a", "/cart/b", "/cart/c"):
                next_path = "/cart/a"
            return self._response(200, (
                "<!doctype html><html><head>"
                f"<meta http-equiv='refresh' content='0;url={next_path}'>"
                "<title>Signed in</title></head><body><h1>Signed in</h1>"
                f"<a href='{next_path}'>Continue to cart</a></body></html>"
            ), {"Set-Cookie": f"sample_session={token}; HttpOnly; SameSite=Strict; Path=/"})
        if route in ("/cart/a", "/cart/b", "/cart/c") and method == "GET":
            if not self._authenticated(headers):
                return self._response(401, f"<h1>Sign in required</h1><a href='/login?next={route}'>Login</a>")
            if route == "/cart/c":
                return self._response(503, "<h1>Cart service unavailable</h1>")
            total = "$20" if route == "/cart/a" else "$25"
            return self._response(200, (
                "<!doctype html><html><head><title>Cart</title></head><body>"
                "<h1>Cart</h1><p>Item: Sample product</p><p>Unit price: $10</p>"
                f"<p>Quantity: 2</p><p>Cart total: {total}</p></body></html>"
            ))
        return self._response(404, "Not found")


class ControlledSampleServer:
    """Serve the sample only on an ephemeral IPv4 loopback port."""

    def __init__(self):
        self.app = SampleApplication()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        if self._server is not None:
            raise RuntimeError("Sample server is already running")
        app = self.app

        class Handler(BaseHTTPRequestHandler):
            def _handle(self):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self.send_error(400)
                    return
                if length < 0 or length > 8192:
                    self.send_error(413)
                    return
                response = app.handle(method=self.command, path=self.path, headers=dict(self.headers),
                                      body=self.rfile.read(length))
                self.send_response(response.status)
                for name, value in response.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(response.body)))
                self.end_headers()
                self.wfile.write(response.body)

            do_GET = _handle
            do_POST = _handle

            def log_message(self, format, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self._server.server_port}"

    def reset(self) -> None:
        self.app.reset()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            if self._thread is not None:
                self._thread.join(timeout=5)
            self._server = None
            self._thread = None
