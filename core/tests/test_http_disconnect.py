"""A client hanging up (Windows: ConnectionAbortedError [WinError 10053]) is not an error: no
traceback, at most one debug line. Other exceptions keep their traceback."""

from __future__ import annotations

import contextlib
import io
import os
import queue
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from smcore import server as srv  # noqa: E402


class Events:
    def __init__(self):
        self.q = queue.Queue()

    def subscribe(self):
        return self.q

    def unsubscribe(self, q):
        pass


class FakeApp:
    def __init__(self, home):
        self.lines = []
        self.events = Events()

        class P:
            pass
        self.paths = P()
        self.paths.home = home

    def log(self, text):
        self.lines.append(text)


class DisconnectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ui = os.path.join(self.tmp, "ui")
        os.makedirs(self.ui)
        with open(os.path.join(self.ui, "index.html"), "wb") as f:
            f.write(b"x" * (64 * 1024 * 1024))   # big enough that the client leaves mid-response
        self.app = FakeApp(self.tmp)
        self.server, self.token = srv.serve(self.app, port=0, ui_dir=self.ui)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def abort(self, path):
        s = socket.create_connection(("127.0.0.1", self.port))
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\nX-SM-Token: {self.token}\r\n\r\n".encode())
        s.recv(1024)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))   # RST, like an abort
        s.close()

    def test_handle_error_quiet_for_disconnects_loud_for_the_rest(self):
        os.environ["GHOSTLABEL_DEBUG"] = "1"
        self.addCleanup(os.environ.pop, "GHOSTLABEL_DEBUG", None)
        for error in (ConnectionAbortedError(10053, "An established connection was aborted"),
                      ConnectionResetError(), BrokenPipeError()):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                try:
                    raise error
                except OSError:
                    self.server.handle_error(None, ("127.0.0.1", 5555))
            self.assertEqual(err.getvalue(), "")
        self.assertEqual(len(self.app.lines), 3)
        self.assertTrue(all("\n" not in line and "disconnected" in line for line in self.app.lines))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                raise ValueError("a real bug")
            except ValueError:
                self.server.handle_error(None, ("127.0.0.1", 5555))
        self.assertIn("Traceback", err.getvalue())
        self.assertIn("ValueError: a real bug", err.getvalue())

    def test_winerror_codes(self):
        e = OSError(0, "aborted")
        e.winerror = 10053
        self.assertTrue(srv.is_disconnect(e))
        self.assertFalse(srv.is_disconnect(OSError(2, "no such file")))

    def test_aborted_downloads_and_event_streams_print_nothing(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for _ in range(3):
                self.abort("/index.html")
            self.abort("/api/events")
            self.app.events.q.put("{}")
            time.sleep(1.0)
            # The server still answers afterwards.
            s = socket.create_connection(("127.0.0.1", self.port))
            s.sendall(f"GET /api/nope HTTP/1.1\r\nHost: x\r\nX-SM-Token: {self.token}\r\nConnection: close\r\n\r\n".encode())
            reply = s.recv(200)
            s.close()
        self.assertIn(b"404", reply.split(b"\r\n")[0])
        self.assertNotIn("Traceback", err.getvalue())
        self.assertNotIn("Exception occurred", err.getvalue())


if __name__ == "__main__":
    unittest.main()
