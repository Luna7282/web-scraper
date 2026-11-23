"""Installs the test-wide network guard (tests/netguard.py) -- discovery
imports this module first, before any test runs -- and proves it works.

Addresses used are never contacted: the guard raises before connecting.
192.0.2.1 is TEST-NET-1 (RFC 5737, reserved for documentation).
"""

import socket
import subprocess
import sys
import threading
import unittest

try:  # `discover -s tests` puts tests/ on sys.path; `-m unittest tests.x` uses the package
    import netguard
except ImportError:  # pragma: no cover
    from tests import netguard

netguard.install_for_suite()

UNROUTABLE = ("192.0.2.1", 80)


class TestNetworkGuard(unittest.TestCase):
    def expect_violation(self):
        self._netguard_expected = True  # this test trips the guard on purpose

    def test_an_outbound_connection_fails_at_once(self):
        self.expect_violation()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(netguard.NetworkBlocked) as ctx:
                s.connect(UNROUTABLE)
        finally:
            s.close()
        self.assertIn("connect to 192.0.2.1:80", str(ctx.exception))
        with self.assertRaises(netguard.NetworkBlocked):
            socket.create_connection(UNROUTABLE, timeout=1)

    def test_dns_for_a_real_name_is_blocked(self):
        self.expect_violation()
        with self.assertRaises(netguard.NetworkBlocked):
            socket.getaddrinfo("example.com", 443)

    def test_localhost_stays_allowed(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        accepted = threading.Thread(target=lambda: server.accept()[0].close(), daemon=True)
        accepted.start()
        client = socket.create_connection(server.getsockname(), timeout=5)
        accepted.join(timeout=5)  # close the server only after accept() returned
        client.close()
        server.close()
        socket.getaddrinfo("localhost", 80)

    def test_a_swallowed_violation_still_fails_the_test(self):
        """Code that catches the exception (e.g. a fetch's broad except) can't
        hide the attempt: the test fails afterwards."""
        self.expect_violation()

        class Swallows(unittest.TestCase):
            def test_it(self):
                try:
                    socket.create_connection(UNROUTABLE, timeout=1)
                except Exception:  # noqa: BLE001, S110 -- swallowed on purpose
                    pass

        result = unittest.TestResult()
        Swallows("test_it").run(result)
        self.assertEqual(len(result.failures), 1)
        self.assertIn("network guard", result.failures[0][1])

    def test_a_child_python_process_is_guarded_too(self):
        self.expect_violation()
        before = len(netguard.read_child_log())
        done = subprocess.run([sys.executable, "-c", "import socket; socket.create_connection(('192.0.2.1', 80), 1)"],
                              capture_output=True, text=True, timeout=60, check=False)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("NetworkBlocked", done.stderr)
        self.assertEqual(len(netguard.read_child_log()), before + 1)

    def test_a_browser_launch_is_blocked(self):
        self.expect_violation()
        with self.assertRaises(netguard.NetworkBlocked):
            subprocess.Popen(["/nonexistent/ms-playwright/chromium-1187/chrome-mac/Chromium", "--headless"])

    def test_a_marked_test_is_allowed(self):
        @netguard.allow_network
        class Marked(unittest.TestCase):
            def test_it(self):
                self.assertTrue(netguard._allowed)  # no real connection is attempted here

        result = unittest.TestResult()
        Marked("test_it").run(result)
        self.assertTrue(result.wasSuccessful(), result.failures)


if __name__ == "__main__":
    unittest.main()
