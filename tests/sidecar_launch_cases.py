# -*- coding: utf-8 -*-
"""A sidecar daemon is launched only when none is there; importing the app
launches nothing.

No network: ports are probed with bind(), Popen is recorded, not run.

    py -3 tests/sidecar_launch_cases.py
"""
import io
import os
import socket
import subprocess
import sys
import unittest
from unittest import mock

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import sidecar_launch as S  # noqa: E402


def _held_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s, s.getsockname()[1]


class LaunchGuardTests(unittest.TestCase):
    def test_a_held_port_is_seen(self):
        s, port = _held_port()
        try:
            self.assertTrue(S.port_taken(port))
        finally:
            s.close()
        self.assertFalse(S.port_taken(port))

    def test_no_launch_while_the_port_is_held(self):
        s, port = _held_port()
        try:
            with mock.patch.object(S.subprocess, "Popen") as popen:
                self.assertFalse(S.start_once(sys.executable, r"C:\nowhere\daemon.py", port))
                popen.assert_not_called()
        finally:
            s.close()

    def test_no_launch_while_a_copy_runs(self):
        # this very test is a running process with its own file on the command line
        _s, port = _held_port()
        _s.close()
        self.assertTrue(S.script_running(os.path.abspath(__file__)))
        with mock.patch.object(S.subprocess, "Popen") as popen, \
                mock.patch.object(S, "script_running", return_value=True):
            self.assertFalse(S.start_once(sys.executable, os.path.abspath(__file__), port))
            popen.assert_not_called()

    def test_launch_when_free_and_none_runs(self):
        _s, port = _held_port()
        _s.close()
        with mock.patch.object(S.subprocess, "Popen") as popen:
            self.assertTrue(S.start_once(sys.executable, r"C:\nowhere\daemon.py", port))
            args, kwargs = popen.call_args
            self.assertEqual(args[0], [sys.executable, r"C:\nowhere\daemon.py"])
            self.assertEqual(kwargs.get("creationflags"), S.DETACHED)

    def test_every_ensure_function_goes_through_the_guard(self):
        from app import lv_morph, messaging
        import inspect
        for fn in (messaging._ensure_nmt_daemon, messaging._ensure_mms_daemon,
                   messaging._ensure_f5_daemon, messaging._ensure_tilde_daemon,
                   lv_morph._ensure_daemon):
            src = inspect.getsource(fn)
            self.assertIn("sidecar_launch.start_once", src, fn.__name__)
            self.assertNotIn("subprocess.Popen", src, fn.__name__)


PROBE = r"""
import subprocess, sys, threading
sys.path.insert(0, %r)
calls = []
real = subprocess.Popen
def rec(args, *a, **k):
    calls.append(" ".join(map(str, args)) if isinstance(args, (list, tuple)) else str(args))
    return real(args, *a, **k)
subprocess.Popen = rec
before = threading.active_count()
from app import main, messaging
print("POPEN", [c for c in calls if "daemon.py" in c])
print("SENDQ", messaging._send_queue_worker["thread"] is None)
print("CATCHUP", messaging._catchup_state["thread"] is None)
print("THREADS", threading.active_count() - before)
"""


class ImportStartsNothingTests(unittest.TestCase):
    def test_importing_main_launches_no_daemon_and_no_worker(self):
        out = subprocess.run([sys.executable, "-X", "utf8", "-c", PROBE % ROOT], cwd=ROOT,
                             capture_output=True, timeout=180)
        text = out.stdout.decode("utf-8", "replace")
        self.assertIn("POPEN []", text, text + out.stderr.decode("utf-8", "replace")[-2000:])
        self.assertIn("SENDQ True", text)
        self.assertIn("CATCHUP True", text)


if __name__ == "__main__":
    unittest.main()
