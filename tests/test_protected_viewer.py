"""Viewer access-control contract using an in-process display double."""

import unittest


class DisplayDouble:
    def __init__(self):
        self.sessions = []
        self.keys = []
        self.clipboard = []
        self.files = []

    def open(self):
        session = object()
        self.sessions.append(session)
        return session

    def key(self, session, key):
        self.keys.append((session, key))

    def set_clipboard(self, session, value):
        self.clipboard.append(value)

    def send_file(self, session, name, content):
        self.files.append((name, content))


def viewer(display, **changes):
    from scrappy_doo.protected_viewer import ProtectedViewer
    options = {"display": display, "bind_host": "127.0.0.1", "token": "a-secure-test-token-with-entropy"}
    options.update(changes)
    return ProtectedViewer(**options)


class ProtectedViewerTests(unittest.TestCase):
    def setUp(self):
        self.display = DisplayDouble()

    def test_viewer_refuses_non_loopback_bind_addresses(self):
        for address in ("0.0.0.0", "192.168.1.2", "::", "viewer.example"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                viewer(self.display, bind_host=address)
        self.assertEqual(self.display.sessions, [])

    def test_missing_or_wrong_authentication_never_opens_a_display_session(self):
        screen = viewer(self.display)
        for token in (None, "", "wrong-token"):
            with self.subTest(token=token):
                self.assertIsNone(screen.connect(peer_host="127.0.0.1", token=token))
        self.assertEqual(self.display.sessions, [])

    def test_non_loopback_client_is_rejected_even_with_a_valid_token(self):
        screen = viewer(self.display)
        self.assertIsNone(screen.connect(peer_host="192.168.1.2", token="a-secure-test-token-with-entropy"))
        self.assertEqual(self.display.sessions, [])

    def test_authenticated_local_session_can_send_keys_for_manual_sign_in(self):
        screen = viewer(self.display)
        session = screen.connect(peer_host="127.0.0.1", token="a-secure-test-token-with-entropy")
        self.assertIsNotNone(session)
        self.assertTrue(screen.send_key(session, "Tab"))
        self.assertEqual(len(self.display.keys), 1)

    def test_clipboard_and_file_transfer_are_disabled_by_default(self):
        screen = viewer(self.display)
        session = screen.connect(peer_host="127.0.0.1", token="a-secure-test-token-with-entropy")
        self.assertFalse(screen.set_clipboard(session, "private text"))
        self.assertFalse(screen.send_file(session, "data.txt", b"private data"))
        self.assertEqual(self.display.clipboard, [])
        self.assertEqual(self.display.files, [])

    def test_unauthenticated_or_closed_session_cannot_send_input(self):
        screen = viewer(self.display)
        self.assertFalse(screen.send_key(object(), "Tab"))
        session = screen.connect(peer_host="127.0.0.1", token="a-secure-test-token-with-entropy")
        screen.disconnect(session)
        self.assertFalse(screen.send_key(session, "Tab"))
        self.assertEqual(self.display.keys, [])

    def test_authentication_secret_is_not_exposed_in_viewer_representation(self):
        secret = "a-secure-test-token-with-entropy"
        screen = viewer(self.display, token=secret)
        self.assertNotIn(secret, repr(screen))


if __name__ == "__main__":
    unittest.main()
