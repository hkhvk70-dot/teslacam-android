"""Connection metadata contract: authenticated, socket based, no guessed P2P."""
import io
import os
import subprocess
import sys
import json
import unittest
from unittest.mock import patch

from test_teslacam_live_v4 import SERVICE


class Socket:
    def __init__(self, request):
        self.request = io.BytesIO(request)
        self.response = bytearray()

    def makefile(self, mode, *args):
        assert mode == "rb"
        return self.request

    def sendall(self, data):
        self.response.extend(data)


def request(peer, cookie="tc=test-only-fixture", extra="", method="GET"):
    sock = Socket((f"{method} /api/connection HTTP/1.1\r\nHost: test.invalid\r\n"
                   f"Cookie: {cookie}\r\nConnection: close\r\n{extra}\r\n").encode())
    with patch.object(SERVICE, "TOKEN", "test-only-fixture"):
        SERVICE.H(sock, (peer, 32123), None)
    head, body = bytes(sock.response).split(b"\r\n\r\n", 1)
    return head.decode(), json.loads(body) if body else None


class ConnectionTests(unittest.TestCase):
    def test_startup_requires_access_code(self):
        environment = dict(os.environ, TOKEN="", PORT="8081")
        result = subprocess.run([sys.executable, "-B", str(SERVICE.__file__)], env=environment, capture_output=True, text=True, timeout=8)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TOKEN is required", result.stderr)

    def test_peer_families_and_boundaries(self):
        cases = {
            "100.96.0.2": "tailnet", "::ffff:100.96.0.2": "tailnet",
            "100.64.0.0": "tailnet", "100.127.255.255": "tailnet",
            "100.63.255.255": "external", "100.128.0.0": "external",
            "fd7a:115c:a1e0::1234": "tailnet", "fd7a:115c:a1e1::1": "lan",
            "127.0.0.1": "forwarded", "::ffff:127.0.0.1": "forwarded", "::1": "forwarded",
            "172.20.0.2": "lan", "192.168.1.20": "lan", "10.1.2.3": "lan",
            "fe80::1%wlan0": "lan", "8.8.8.8": "external", "2001:4860::1": "external",
            "bad": "unknown", "": "unknown",
        }
        for peer, expected in cases.items():
            with self.subTest(peer=peer):
                self.assertEqual(SERVICE.connection_access(peer), expected)

    def test_forwarding_headers_cannot_fake_tailnet_or_direct(self):
        head, data = request("127.0.0.1", extra="X-Forwarded-For: 100.96.0.2\r\nX-Tailscale-Status: direct\r\n")
        self.assertIn("200 OK", head)
        self.assertEqual(data["access"], "forwarded")
        self.assertEqual(data["peer_path"], "unknown")

    def test_tailnet_is_never_claimed_as_p2p(self):
        head, data = request("::ffff:100.96.0.2")
        self.assertEqual(data["access"], "tailnet")
        self.assertEqual(data["peer_path"], "unknown")
        self.assertEqual(set(data), {"access", "peer_path", "observed_at"})
        self.assertIn("Cache-Control: no-store", head)

    def test_auth_is_required(self):
        for cookie in ("", "tc=wrong"):
            with self.subTest(cookie=cookie):
                head, data = request("100.96.0.2", cookie=cookie)
                self.assertIn("401 Unauthorized", head)
                self.assertEqual(data["status"], "unauthorized")
                self.assertNotIn("access", data)

    def test_head_has_no_body(self):
        head, data = request("100.96.0.2", method="HEAD")
        self.assertIn("200 OK", head)
        self.assertIsNone(data)


if __name__ == "__main__":
    unittest.main()
