"""Local viewer session authorization, independent of a display transport.

This is not a network listener or display server. A future transport must use
an authenticated peer address and must not expose clipboard or file transfer.
"""

import ipaddress
import secrets


class ProtectedViewer:
    def __init__(self, *, display, bind_host, token):
        try:
            if not ipaddress.ip_address(bind_host).is_loopback:
                raise ValueError("Viewer must bind to loopback")
        except (TypeError, ValueError):
            raise ValueError("Viewer must bind to loopback") from None
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("Viewer token must be at least 32 characters")
        self._display = display
        self._token = token
        self._sessions = {}

    def connect(self, *, peer_host, token):
        try:
            local = ipaddress.ip_address(peer_host).is_loopback
        except (TypeError, ValueError):
            return None
        if not local or not isinstance(token, str) or not secrets.compare_digest(token, self._token):
            return None
        display_session = self._display.open()
        handle = object()
        self._sessions[handle] = display_session
        return handle

    def send_key(self, session, key):
        if session not in self._sessions or not isinstance(key, str):
            return False
        self._display.key(self._sessions[session], key)
        return True

    def set_clipboard(self, session, value):
        return False

    def send_file(self, session, name, content):
        return False

    def disconnect(self, session):
        self._sessions.pop(session, None)
