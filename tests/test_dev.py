"""Development services start only after their dependencies are ready."""

import socket
import subprocess
import sys
import unittest

import dev


class DevTests(unittest.TestCase):
    def test_waits_until_server_accepts_connections(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            address = server.getsockname()
            with subprocess.Popen(
                [sys.executable, "-c", """
import socket, sys, time
server = socket.socket(fileno=int(sys.argv[1]))
time.sleep(0.1)
server.listen()
print('ready', flush=True)
sys.stdin.read()
""", str(server.fileno())],
                pass_fds=(server.fileno(),), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, text=True,
            ) as process:
                try:
                    dev.wait_for_server(process, *address, timeout=5)
                    self.assertEqual(process.stdout.readline().strip(), "ready")
                    self.assertIsNone(process.poll())
                finally:
                    process.stdin.close()
                    process.wait(timeout=5)

    def test_reports_backend_exit_without_waiting_for_port(self):
        with subprocess.Popen([sys.executable, "-c", "raise SystemExit(7)"]) as process:
            process.wait(timeout=5)
            with self.assertRaisesRegex(RuntimeError, "7"):
                dev.wait_for_server(process, "127.0.0.1", 0, timeout=5)

    def test_stops_waiting_when_server_never_listens(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            with subprocess.Popen(
                [sys.executable, "-c", "import sys; sys.stdin.read()"],
                stdin=subprocess.PIPE,
            ) as process:
                try:
                    with self.assertRaises(TimeoutError):
                        dev.wait_for_server(process, *server.getsockname(), timeout=0)
                finally:
                    process.stdin.close()
                    process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
